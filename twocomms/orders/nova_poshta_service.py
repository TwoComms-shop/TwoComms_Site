"""
Сервис для работы с API Новой Почты

Этот модуль обеспечивает интеграцию с Nova Poshta API для отслеживания статусов посылок.
Основные функции:
- Получение статуса посылки по ТТН
- Автоматическое обновление статусов заказов
- Отправка уведомлений в Telegram и Facebook
- Обработка ошибок API с детальным логированием
"""
import requests
import json
import logging
import time
from datetime import datetime, timedelta
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import close_old_connections, transaction
from django.db.models import F, Q
from .models import Order
from .fulfillment_truth import NOVA_POSHTA_DELIVERY_SUCCESS_CODES
from .telegram_notifications import TelegramNotifier

logger = logging.getLogger(__name__)


class NovaPoshtaAPIError(Exception):
    """Ошибка при работе с Nova Poshta API"""


class NovaPoshtaService:
    """
    Сервис для работы с API Новой Почты

    Документация API: https://api.novapost.com/developers/index.html#overview

    Основные методы:
    - get_tracking_info(ttn_number) - получить статус посылки
    - update_order_tracking_status(order) - обновить статус заказа
    - update_all_tracking_statuses() - обновить все заказы с ТТН
    """

    API_URL = "https://api.novaposhta.ua/v2.0/json/"

    # Коды StatusCode из актуального контракта Nova Poshta. Нельзя сравнивать
    # их численно: 101 -- промежуточное движение, а 10/11 -- этапы успешного
    # получения с денежным переводом.
    STATUS_READY_TO_SEND = 1
    STATUS_DELETED = 2
    STATUS_NOT_FOUND = 3
    STATUS_ACCEPTED = 4
    STATUS_SENT = 5
    STATUS_ARRIVED_CITY = 6
    STATUS_ARRIVED_WAREHOUSE = 7
    STATUS_ARRIVED_POSTOMAT = 8
    STATUS_RECEIVED = 9
    STATUS_MONEY_TRANSFER_SENT = 10
    STATUS_MONEY_TRANSFER_RECEIVED = 11
    STATUS_RECEIVED_OLD = STATUS_RECEIVED
    STATUS_SENT_ALT = STATUS_SENT
    STATUS_UNKNOWN = 999
    STATUS_RETURNED = 102
    STATUS_REFUSED = 103
    STATUS_REFUSED_ALT = STATUS_REFUSED

    DELIVERY_SUCCESS_CODES = NOVA_POSHTA_DELIVERY_SUCCESS_CODES
    TERMINAL_FAILURE_CODES = frozenset({2, 103, 105, 118, 130, 155})
    WAITING_CHECK_CODES = frozenset({7, 8, 99, 102, 104, 106, 110, 111, 112, 113, 114, 115, 116, 131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149})
    TRACKING_BATCH_SIZE = 100
    TRACKING_MAX_AGE = timedelta(days=90)
    ACTIVE_CHECK_INTERVAL = timedelta(minutes=5)
    WAITING_CHECK_INTERVAL = timedelta(minutes=15)

    # Настройки повторных запросов
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # секунды
    REQUEST_TIMEOUT = 10  # секунды

    # Настройки rate limiting
    RATE_LIMIT_KEY = 'nova_poshta_api_calls'
    RATE_LIMIT_MAX_CALLS = 60  # максимум вызовов
    RATE_LIMIT_PERIOD = 60  # за период в секундах

    # Ключи кеша
    LAST_UPDATE_CACHE_KEY = 'nova_poshta_last_update'
    UPDATE_LOCK_CACHE_KEY = 'nova_poshta_update_lock'
    FALLBACK_CHECK_MULTIPLIER = 3  # fallback после N интервалов cron
    UPDATE_LOCK_TIMEOUT = 10 * 60  # 10 минут

    def __init__(self):
        self.api_key = getattr(settings, 'NOVA_POSHTA_API_KEY', '')
        self.api_url = getattr(settings, 'NOVA_POSHTA_API_URL', self.API_URL)
        self.telegram_notifier = TelegramNotifier()

        if not self.api_key:
            logger.warning("NOVA_POSHTA_API_KEY не настроен в settings")
        if self.api_url != self.API_URL:
            logger.debug(f"Using custom Nova Poshta API URL: {self.api_url}")

    def _check_rate_limit(self):
        """
        Проверяет и применяет rate limiting для API запросов

        Returns:
            bool: True если запрос можно выполнить, False если лимит превышен
        """
        current_calls = cache.get(self.RATE_LIMIT_KEY, 0)

        if current_calls >= self.RATE_LIMIT_MAX_CALLS:
            logger.warning(
                f"Rate limit exceeded: {current_calls}/{self.RATE_LIMIT_MAX_CALLS} "
                f"calls in {self.RATE_LIMIT_PERIOD}s"
            )
            return False

        # Увеличиваем счетчик
        cache.set(
            self.RATE_LIMIT_KEY,
            current_calls + 1,
            self.RATE_LIMIT_PERIOD
        )
        return True

    def get_tracking_info(self, ttn_number, phone=None):
        """
        Получает информацию о статусе посылки по ТТН

        Использует метод API: TrackingDocument.getStatusDocuments
        Документация: https://api.novapost.com/developers/index.html#tracking

        Args:
            ttn_number (str): Номер ТТН (накладной)
            phone (str, optional): Телефон для более точного поиска

        Returns:
            dict: Информация о посылке с полями:
                - Number: номер ТТН
                - Status: текстовый статус
                - StatusCode: числовой код статуса (9 = получено)
                - StatusDescription: описание статуса
                - DateCreated: дата создания
                - DateLastMovementStatus: дата последнего изменения
                и другие поля
            None: В случае ошибки или если посылка не найдена

        Raises:
            NovaPoshtaAPIError: При критических ошибках API
        """
        if not ttn_number or not self.api_key:
            logger.warning(
                f"Cannot get tracking info: "
                f"ttn_number={'present' if ttn_number else 'missing'}, "
                f"api_key={'present' if self.api_key else 'missing'}"
            )
            return None

        # Проверяем rate limit
        if not self._check_rate_limit():
            logger.error(f"Rate limit exceeded for TTN {ttn_number}")
            return None

        logger.debug(f"Requesting tracking info for TTN: {ttn_number}")

        payload = {
            "apiKey": self.api_key,
            "modelName": "TrackingDocument",
            "calledMethod": "getStatusDocuments",
            "methodProperties": {
                "Documents": [
                    {
                        "DocumentNumber": ttn_number,
                        "Phone": phone or ""
                    }
                ]
            }
        }

        # Попытки с повторными запросами при сетевых ошибках
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                logger.debug(f"API request attempt {attempt + 1}/{self.MAX_RETRIES}")

                response = requests.post(
                    self.api_url,
                    json=payload,
                    timeout=self.REQUEST_TIMEOUT
                )
                response.raise_for_status()

                data = response.json()
                logger.debug(f"API response for TTN {ttn_number}: {json.dumps(data, ensure_ascii=False)}")

                # Проверяем наличие ошибок в ответе API
                if data.get('errors') and len(data.get('errors', [])) > 0:
                    errors = data.get('errors', [])
                    error_msg = ', '.join(str(e) for e in errors)
                    logger.error(f"Nova Poshta API errors for TTN {ttn_number}: {error_msg}")
                    return None

                # Проверяем предупреждения (warnings)
                if data.get('warnings') and len(data.get('warnings', [])) > 0:
                    warnings = data.get('warnings', [])
                    warning_msg = ', '.join(str(w) for w in warnings)
                    logger.warning(f"Nova Poshta API warnings for TTN {ttn_number}: {warning_msg}")

                # Проверяем успешность запроса
                if not data.get('success'):
                    logger.warning(f"API returned success=false for TTN {ttn_number}")
                    return None

                # Проверяем наличие данных
                if not data.get('data'):
                    logger.warning(f"No data in API response for TTN {ttn_number}")
                    return None

                # Обрабатываем данные (может быть массив или объект)
                tracking_data = None
                if isinstance(data['data'], list):
                    if len(data['data']) == 0:
                        logger.warning(f"Empty data array for TTN {ttn_number}")
                        return None
                    tracking_data = data['data'][0]
                elif isinstance(data['data'], dict):
                    tracking_data = data['data']
                else:
                    logger.error(
                        f"Unexpected data type for TTN {ttn_number}: "
                        f"{type(data['data'])}"
                    )
                    return None

                # Логируем полученную информацию
                status = tracking_data.get('Status', 'Unknown')
                status_code = tracking_data.get('StatusCode')
                status_description = tracking_data.get('StatusDescription', '')

                logger.info(
                    f"Tracking info for TTN {ttn_number}: "
                    f"Status='{status}', StatusCode={status_code}, "
                    f"Description='{status_description}'"
                )

                return tracking_data

            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning(
                    f"Timeout error for TTN {ttn_number} "
                    f"(attempt {attempt + 1}/{self.MAX_RETRIES}): {e}"
                )
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY * (attempt + 1))

            except requests.exceptions.RequestException as e:
                last_error = e
                logger.error(
                    f"Network error for TTN {ttn_number} "
                    f"(attempt {attempt + 1}/{self.MAX_RETRIES}): {e}"
                )
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY * (attempt + 1))

            except ValueError as e:
                last_error = e
                logger.error(f"JSON parsing error for TTN {ttn_number}: {e}")
                return None

            except Exception as e:
                last_error = e
                logger.exception(f"Unexpected error for TTN {ttn_number}: {e}")
                return None

        # Все попытки исчерпаны
        logger.error(
            f"Failed to get tracking info for TTN {ttn_number} "
            f"after {self.MAX_RETRIES} attempts. Last error: {last_error}"
        )
        return None

    @staticmethod
    def _tracking_key(number):
        return "".join(ch for ch in str(number or "").strip() if ch.isalnum()).casefold()

    @staticmethod
    def _provider_event_datetime(item):
        raw = item.get("DateLastMovementStatus") or item.get("DateLastMovement")
        if not raw:
            return None
        parsed = parse_datetime(str(raw).replace("Z", "+00:00"))
        if parsed is not None:
            return parsed
        for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                return timezone.make_aware(datetime.strptime(str(raw), fmt))
            except (TypeError, ValueError):
                continue
        return None

    def get_tracking_info_batch(self, documents):
        """Fetch at most 100 tracking documents and index them by TTN."""
        documents = [
            {
                "DocumentNumber": str(item.get("DocumentNumber") or "").strip(),
                "Phone": str(item.get("Phone") or "").strip(),
            }
            for item in (documents or [])
            if str(item.get("DocumentNumber") or "").strip()
        ]
        if not documents:
            return {}
        if len(documents) > self.TRACKING_BATCH_SIZE:
            raise NovaPoshtaAPIError("Nova Poshta tracking batch cannot contain more than 100 documents")
        requested_keys = {self._tracking_key(item["DocumentNumber"]) for item in documents}
        if not self.api_key:
            raise NovaPoshtaAPIError("NOVA_POSHTA_API_KEY not configured")
        if not self._check_rate_limit():
            raise NovaPoshtaAPIError("Nova Poshta tracking rate limit exceeded")

        payload = {
            "apiKey": self.api_key,
            "modelName": "TrackingDocument",
            "calledMethod": "getStatusDocuments",
            "methodProperties": {"Documents": documents},
        }
        session = getattr(self, "_tracking_session", None)
        owned_session = session is None
        session = session or requests.Session()
        last_error = None
        try:
            for attempt in range(self.MAX_RETRIES):
                try:
                    response = session.post(self.api_url, json=payload, timeout=self.REQUEST_TIMEOUT)
                    response.raise_for_status()
                    data = response.json()
                    errors = [str(item).strip() for item in data.get("errors") or [] if str(item).strip()]
                    if errors:
                        raise NovaPoshtaAPIError("; ".join(errors))
                    if not data.get("success"):
                        raise NovaPoshtaAPIError("Nova Poshta tracking returned success=false")
                    raw_items = data.get("data") or []
                    if isinstance(raw_items, dict):
                        raw_items = [raw_items]
                    if not isinstance(raw_items, list):
                        raise NovaPoshtaAPIError("Nova Poshta tracking returned invalid data")

                    indexed = {}
                    for item in raw_items:
                        if not isinstance(item, dict):
                            continue
                        key = self._tracking_key(item.get("Number") or item.get("DocumentNumber"))
                        if not key or key not in requested_keys:
                            continue
                        previous = indexed.get(key)
                        if previous is None:
                            indexed[key] = item
                            continue
                        previous_at = self._provider_event_datetime(previous)
                        current_at = self._provider_event_datetime(item)
                        if current_at is not None and (previous_at is None or current_at >= previous_at):
                            indexed[key] = item
                    return indexed
                except NovaPoshtaAPIError:
                    raise
                except (requests.exceptions.Timeout, requests.exceptions.RequestException, ValueError) as exc:
                    last_error = exc
                    logger.warning(
                        "Nova Poshta tracking batch attempt %s/%s failed: %s",
                        attempt + 1,
                        self.MAX_RETRIES,
                        exc,
                    )
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(self.RETRY_DELAY * (attempt + 1))
            raise NovaPoshtaAPIError("Nova Poshta tracking request failed") from last_error
        finally:
            if owned_session:
                session.close()

    # Длина поля Order.shipment_status (CharField). Текст длиннее усекается,
    # чтобы не падало сохранение и не ломалось сравнение статусов.
    SHIPMENT_STATUS_MAX_LENGTH = 100

    @staticmethod
    def _normalize_status_code(raw_code, order_number=""):
        """Приводит StatusCode к int или None (NP иногда отдаёт строку)."""
        if raw_code is None:
            return None
        try:
            return int(raw_code)
        except (TypeError, ValueError):
            logger.warning(
                f"Order {order_number}: unexpected StatusCode format ({raw_code})"
            )
            return None

    def _status_indicates_delivered(self, status, status_description, status_code=None):
        """
        Чистая проверка «посылка получена» без сайд-эффектов.

        Nova Poshta's numeric StatusCode is the only fulfillment truth. Text
        is localized and can describe a storage/payment note while the parcel
        is still in transit, so it must never trigger a delivered hook.
        """
        return status_code in self.DELIVERY_SUCCESS_CODES

    @staticmethod
    def _has_persisted_purchase_evidence(order):
        """Detect a Purchase already established by a payment-side channel."""
        payload = getattr(order, "payment_payload", None)
        if not isinstance(payload, dict):
            return False

        facebook_events = payload.get("facebook_events")
        if isinstance(facebook_events, dict) and facebook_events.get("purchase_sent"):
            return True
        capi = payload.get("fb_conversions_api")
        if isinstance(capi, dict) and capi.get("event_id"):
            return True
        tiktok_events = payload.get("tiktok_events")
        if isinstance(tiktok_events, dict) and tiktok_events.get("purchase_sent"):
            return True

        channels = payload.get("post_payment_channels")
        if isinstance(channels, dict):
            for channel_name in ("meta_purchase", "tiktok_purchase"):
                channel = channels.get(channel_name)
                if not isinstance(channel, dict):
                    continue
                if channel.get("state") == "sent" or channel.get("event_id"):
                    return True
        return False

    @classmethod
    def _is_legacy_web_cod_purchase(cls, order):
        """Return whether delivery may be the historical website purchase moment.

        Online and Instagram checkouts already establish Purchase at the
        payment/thank-you boundary.  Nova Poshta is allowed to emit the
        fallback only for the old website cash-on-delivery contract.
        """
        return (
            str(getattr(order, "source", "") or "").strip().lower() == "web"
            and str(getattr(order, "pay_type", "") or "").strip().lower() == "cod"
            and not cls._has_persisted_purchase_evidence(order)
        )

    def update_order_tracking_status(self, order):
        """
        Обновляет статус посылки для заказа.

        Алгоритм:
        1. Тянет статус через API (вне блокировки).
        2. В одной транзакции с ``select_for_update`` берёт row-lock на заказ,
           перечитывает актуальное состояние, решает по StatusCode нужно ли
           уведомление, меняет поля и фиксирует якорь дедупа.
        3. После коммита (вне блокировки) шлёт Telegram/Facebook уведомления.

        Row-lock гарантирует, что параллельные потоки/процессы (несколько
        worker'ов Passenger + middleware fallback) не обработают один заказ
        одновременно и не зашлют дубликаты — второй поток дождётся коммита
        первого и увидит уже обновлённый якорь.

        Returns:
            bool: True если что-то изменилось/отправлено, False если нет
        """
        if not order.tracking_number:
            logger.debug(f"Order {order.order_number}: no tracking number")
            return False

        logger.info(f"Updating tracking status for order {order.order_number}")

        tracking_info = self.get_tracking_info(
            order.tracking_number,
            phone=getattr(order, "phone", None),
        )

        if not tracking_info:
            logger.warning(f"Failed to get tracking info for order {order.order_number}")
            return False

        return self._apply_tracking_info_to_order(order, tracking_info)

    def _apply_tracking_info_to_order(self, order, tracking_info):
        """Apply one provider result; external notifications stay outside DB locks."""
        self._last_tracking_update_error = False
        if not tracking_info:
            return False

        status = (tracking_info.get('Status', '') or '').strip()
        status_description = (tracking_info.get('StatusDescription', '') or '').strip()
        status_code = self._normalize_status_code(
            tracking_info.get('StatusCode'), order.order_number
        )
        delivered_status = self._status_indicates_delivered(
            status,
            status_description,
            status_code,
        )

        # Полное описание для отображения, усечённое под длину поля
        full_status = f"{status} - {status_description}" if status_description else status
        full_status = full_status.strip()[:self.SHIPMENT_STATUS_MAX_LENGTH]

        try:
            close_old_connections()
            decision = self._apply_tracking_update(
                order.pk,
                status,
                status_description,
                status_code,
                full_status,
                provider_event_at=self._provider_event_datetime(tracking_info),
            )
        except ObjectDoesNotExist:
            logger.warning(f"Order pk={order.pk} disappeared during tracking update")
            return False
        except Exception as e:
            self._last_tracking_update_error = True
            logger.error(
                f"Order {order.order_number}: failed to apply tracking update: {e}",
                exc_info=True,
            )
            return False
        finally:
            close_old_connections()

        if decision is None:
            # Notification dedup must not disable analytics healing. If a
            # previous post-commit write failed, a repeated delivered poll is
            # the retry path for COD/manual orders excluded from backfill.
            if delivered_status:
                order.refresh_from_db()
                if order.status == 'done':
                    self._record_purchase_action(order)
                    self._dispatch_ig_delivery_lifecycle(
                        order,
                        status_code=status_code,
                        shipment_status=full_status,
                    )
            logger.debug(f"Order {order.order_number}: no changes")
            return False

        # Синхронизируем переданный объект с зафиксированным состоянием,
        # чтобы вызывающий код и уведомления видели свежие данные.
        fresh = decision['order']
        order.status = fresh.status
        order.payment_status = fresh.payment_status
        order.shipment_status = fresh.shipment_status
        order.shipment_status_updated = fresh.shipment_status_updated
        order.tracking_status_code = fresh.tracking_status_code
        order.tracking_checked_at = fresh.tracking_checked_at
        order.tracking_provider_event_at = fresh.tracking_provider_event_at
        order.tracking_next_check_at = fresh.tracking_next_check_at
        order.tracking_failure_count = fresh.tracking_failure_count
        order.tracking_terminal_at = fresh.tracking_terminal_at
        order.payment_payload = fresh.payment_payload

        if delivered_status and order.status == 'done':
            self._record_purchase_action(order)
            self._dispatch_ig_delivery_lifecycle(
                order,
                status_code=status_code,
                shipment_status=full_status,
            )
        else:
            self._dispatch_ig_parcel_arrived_lifecycle(
                order,
                status_code=status_code,
                shipment_status=full_status,
            )

        if not decision['notify']:
            return decision['changed']

        # --- Уведомления и внешние события: строго вне транзакции/лока ---
        if decision['is_delivery']:
            if decision['payment_status_changed']:
                # Delivery is an advertising conversion only for legacy web
                # COD.  Online/Instagram orders already have the canonical
                # Purchase at payment/Thank You and must never be re-emitted.
                if self._is_legacy_web_cod_purchase(order):
                    self._send_facebook_purchase_event(order)
                    self._send_tiktok_purchase_event(order)
                else:
                    logger.info(
                        "Skipping delivery Purchase for order %s: canonical "
                        "payment/Thank You conversion owns this event",
                        order.order_number,
                    )
            self._send_admin_delivery_notification(
                order, decision['old_order_status'], decision['payment_status_changed']
            )
            self._send_delivery_notification(order, full_status)
        else:
            self._send_status_notification(order, decision['old_shipment_status'], full_status)

        return True

    # Код 7 Нової Пошти — «прибуло у відділення». Це єдина точка між «ТТН
    # створено» і «отримано», на якій ще можна запобігти поверненню посилки.
    NP_ARRIVED_AT_BRANCH_CODES = frozenset({7})

    @classmethod
    def _dispatch_ig_parcel_arrived_lifecycle(cls, order, *, status_code=None, shipment_status=''):
        """Project a branch arrival into Instagram Direct exactly once per TTN."""
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            return
        if code not in cls.NP_ARRIVED_AT_BRANCH_CODES:
            return
        tracking_number = str(getattr(order, 'tracking_number', '') or '').strip()
        if not tracking_number:
            return
        try:
            from management.ig_bot_models import IgLifecycleEvent
            from management.services.ig_lifecycle import (
                dispatch_lifecycle_event,
                ensure_lifecycle_event,
            )

            event, _created = ensure_lifecycle_event(
                order,
                IgLifecycleEvent.Kind.PARCEL_ARRIVED,
                payload={
                    'tracking_number': tracking_number,
                    'status_code': str(code),
                    'status': str(shipment_status or '')[:300],
                },
            )
            if event is not None:
                dispatch_lifecycle_event(event.pk)
        except Exception:
            logger.exception(
                'Failed to project parcel arrival Instagram lifecycle for order %s',
                getattr(order, 'pk', None),
            )

    @staticmethod
    def _dispatch_ig_delivery_lifecycle(order, *, status_code=None, shipment_status=''):
        """Project a committed Nova Poshta delivery into Instagram Direct."""
        try:
            from management.ig_bot_models import IgLifecycleEvent
            from management.services.ig_lifecycle import (
                dispatch_lifecycle_event,
                ensure_lifecycle_event,
            )

            event, _created = ensure_lifecycle_event(
                order,
                IgLifecycleEvent.Kind.DELIVERED_REVIEW_REQUESTED,
                payload={
                    "status_code": str(status_code or "delivered"),
                    "status": str(shipment_status or "")[:300],
                },
            )
            if event is not None:
                dispatch_lifecycle_event(event.pk)
        except Exception:
            logger.exception(
                "Failed to project delivered Instagram lifecycle for order %s",
                getattr(order, "pk", None),
            )

    def _apply_tracking_update(
        self,
        order_pk,
        status,
        status_description,
        status_code,
        full_status,
        *,
        provider_event_at=None,
    ):
        """
        Атомарно (с row-lock) применяет изменение статуса к заказу.

        Возвращает dict с флагами для последующей отправки уведомлений
        или None, если изменений нет.
        """
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order_pk)

            old_shipment_status = order.shipment_status or ''
            old_order_status = order.status

            current_status_base = old_shipment_status.split(' - ')[0].strip()
            text_changed = current_status_base != status

            payload = order.payment_payload if isinstance(order.payment_payload, dict) else {}
            np_tracking = payload.get('np_tracking')
            if not isinstance(np_tracking, dict):
                np_tracking = {}
            last_notified_code = np_tracking.get('last_status_code')
            has_anchor = 'last_status_code' in np_tracking

            # Решение об уведомлении строится на StatusCode, а не на тексте
            if status_code is not None:
                if not has_anchor:
                    # Якоря ещё нет (старый заказ): не спамим, если базовый
                    # текст не изменился — просто инициализируем якорь.
                    should_notify = text_changed
                else:
                    should_notify = status_code != last_notified_code
            else:
                should_notify = text_changed

            update_fields = []

            now = timezone.now()
            is_terminal = status_code in self.DELIVERY_SUCCESS_CODES or status_code in self.TERMINAL_FAILURE_CODES

            # A terminal provider status is immutable for polling purposes.
            # Avoid reporting a change (and doing a DB write) on every direct
            # retry when Nova Poshta returns the same code and event time.
            if (
                is_terminal
                and order.tracking_terminal_at is not None
                and order.tracking_status_code == status_code
                and not text_changed
                and order.tracking_provider_event_at == provider_event_at
            ):
                return None

            next_check_at = None if is_terminal else now + (
                self.WAITING_CHECK_INTERVAL
                if status_code in self.WAITING_CHECK_CODES
                else self.ACTIVE_CHECK_INTERVAL
            )
            order.tracking_status_code = status_code
            order.tracking_checked_at = now
            order.tracking_provider_event_at = provider_event_at
            order.tracking_next_check_at = next_check_at
            order.tracking_failure_count = 0
            if is_terminal and order.tracking_terminal_at is None:
                order.tracking_terminal_at = now
            elif not is_terminal:
                order.tracking_terminal_at = None
            update_fields += [
                'tracking_status_code',
                'tracking_checked_at',
                'tracking_provider_event_at',
                'tracking_next_check_at',
                'tracking_failure_count',
                'tracking_terminal_at',
            ]

            if text_changed:
                order.shipment_status = full_status
                order.shipment_status_updated = timezone.now()
                update_fields += ['shipment_status', 'shipment_status_updated']

            is_delivery = False
            payment_status_changed = False

            if should_notify:
                if (
                    self._status_indicates_delivered(status, status_description, status_code)
                    and order.status != 'done'
                ):
                    order.status = 'done'
                    is_delivery = True
                    update_fields.append('status')
                    logger.info(
                        f"✅ Order {order.order_number}: status '{old_order_status}' -> 'done' "
                        f"(parcel received, StatusCode={status_code})"
                    )
                    if order.payment_status != 'paid':
                        order.payment_status = 'paid'
                        payment_status_changed = True
                        update_fields.append('payment_status')

                # Фиксируем якорь дедупа по коду
                np_tracking['last_status_code'] = status_code
                np_tracking['last_status_text'] = (full_status or '')[:self.SHIPMENT_STATUS_MAX_LENGTH]
                np_tracking['last_notified_at'] = timezone.now().isoformat()
                payload['np_tracking'] = np_tracking
                order.payment_payload = payload
                update_fields.append('payment_payload')
            elif status_code is not None and status_code != last_notified_code:
                # Молча инициализируем/обновляем якорь без уведомления
                np_tracking['last_status_code'] = status_code
                np_tracking['last_status_text'] = (full_status or '')[:self.SHIPMENT_STATUS_MAX_LENGTH]
                np_tracking['last_notified_at'] = timezone.now().isoformat()
                payload['np_tracking'] = np_tracking
                order.payment_payload = payload
                update_fields.append('payment_payload')

            if not update_fields:
                return None

            order.save(update_fields=update_fields)

            return {
                'order': order,
                'changed': True,
                'notify': should_notify,
                'is_delivery': is_delivery,
                'payment_status_changed': payment_status_changed,
                'old_order_status': old_order_status,
                'old_shipment_status': old_shipment_status,
            }

    def _send_facebook_purchase_event(self, order):
        """
        Отправляет Purchase событие в Facebook Conversions API.
        Legacy-only: текущий storefront COD не продаёт, но этот путь остаётся
        для старых web-заказов и не должен использоваться для новых кампаний.

        Вызывается автоматически когда:
        - посылка получена через Новую Почту;
        - старый web COD-заказ перешёл в ``paid``.

        Args:
            order (Order): Заказ для которого отправляется событие
        """
        try:
            if not self._is_legacy_web_cod_purchase(order):
                logger.info(
                    "Skipping Nova Poshta Facebook Purchase for non-COD order %s",
                    order.order_number,
                )
                return False

            from .facebook_conversions_service import get_facebook_conversions_service

            fb_service = get_facebook_conversions_service()

            if fb_service.enabled:
                payment_payload = order.payment_payload or {}
                facebook_events = payment_payload.get('facebook_events', {})

                if facebook_events.get('purchase_sent'):
                    logger.info(
                        f"📊 Facebook Purchase event already sent for order {order.order_number}, skipping duplicate"
                    )
                    return False

                facebook_events.setdefault(
                    'purchase_event_time',
                    int(timezone.now().timestamp()),
                )
                success = fb_service.send_purchase_event(order)
                if success:
                    facebook_events['purchase_sent'] = True
                    facebook_events['purchase_sent_at'] = timezone.now().isoformat()
                    payment_payload['facebook_events'] = facebook_events
                    order.payment_payload = payment_payload
                    try:
                        order.save(update_fields=['payment_payload'])
                    except Exception:
                        logger.exception(
                            "Failed to persist Facebook purchase flag for order %s",
                            order.order_number,
                        )
                        raise
                    logger.info(f"📊 Facebook Purchase event sent for order {order.order_number}")
                    return True
                else:
                    logger.warning(f"⚠️ Failed to send Facebook Purchase event for order {order.order_number}")
                    return False
            else:
                logger.debug("Facebook Conversions API not enabled, skipping Purchase event")
                return False

        except Exception as e:
            logger.exception(f"❌ Error sending Facebook Purchase event for order {order.order_number}: {e}")
            return False

    def _send_tiktok_purchase_event(self, order):
        """
        W2-3в / AN-014: legacy web COD Purchase при получении посылки.

        Онлайн- и Instagram-заказы уже имеют канонический Purchase в момент
        оплаты/Thank You и не должны повторно попадать в рекламную воронку.
        """
        try:
            if not self._is_legacy_web_cod_purchase(order):
                logger.info(
                    "Skipping Nova Poshta TikTok Purchase for non-COD order %s",
                    order.order_number,
                )
                return False

            from .tiktok_events_service import get_tiktok_events_service

            tiktok_service = get_tiktok_events_service()
            if not tiktok_service.enabled:
                logger.debug("TikTok Events API not enabled, skipping Purchase event")
                return False

            payment_payload = order.payment_payload or {}
            tiktok_events = payment_payload.get('tiktok_events', {})
            if tiktok_events.get('purchase_sent'):
                logger.info(
                    f"📈 TikTok Purchase event already sent for order {order.order_number}, skipping duplicate"
                )
                return False

            success = tiktok_service.send_purchase_event(order)
            if success:
                tiktok_events['purchase_sent'] = True
                tiktok_events['purchase_sent_at'] = timezone.now().isoformat()
                payment_payload['tiktok_events'] = tiktok_events
                order.payment_payload = payment_payload
                try:
                    order.save(update_fields=['payment_payload'])
                except Exception:
                    logger.exception(
                        "Failed to persist TikTok purchase flag for order %s",
                        order.order_number,
                    )
                    raise
                logger.info(f"📈 TikTok Purchase event sent for order {order.order_number} (delivery)")
                return True
            else:
                logger.warning(f"⚠️ Failed to send TikTok Purchase event for order {order.order_number}")
                return False
        except Exception as e:
            logger.exception(f"❌ Error sending TikTok Purchase event for order {order.order_number}: {e}")
            return False

    def _record_purchase_action(self, order):
        """
        W2-3б: внутренний UserAction 'purchase' при получении посылки.

        До фикса COD-выкупы не попадали в UserAction вообще → внутренняя
        воронка (view→ATC→checkout→purchase) не видела большинство покупок.
        Дедуп: не пишем второй purchase для того же заказа.
        """
        try:
            from storefront.utm_tracking import ensure_order_purchase_action

            ensure_order_purchase_action(
                order,
                metadata={'source': 'np_delivery', 'trigger': 'parcel_received'},
            )
        except Exception as e:
            logger.exception(f"❌ Error recording purchase action for order {order.order_number}: {e}")

    def _send_admin_delivery_notification(self, order, old_status, payment_status_changed):
        """
        Отправляет уведомление админу об автоматическом изменении статуса заказа

        Args:
            order (Order): Заказ
            old_status (str): Старый статус заказа
            payment_status_changed (bool): Изменился ли payment_status
        """
        if not self.telegram_notifier.is_configured():
            logger.debug("Telegram notifier not configured, skipping admin notification")
            return

        status_display = {
            'new': 'В обробці',
            'prep': 'Готується до відправлення',
            'ship': 'Відправлено',
            'done': 'Отримано',
            'cancelled': 'Скасовано',
        }

        old_status_text = status_display.get(old_status, old_status)
        new_status_text = status_display.get('done', 'Отримано')

        message = f"""🤖 <b>АВТОМАТИЧНЕ ОНОВЛЕННЯ СТАТУСУ</b>

🆔 <b>Замовлення:</b> #{order.order_number}
📋 <b>ТТН:</b> {order.tracking_number or 'Не вказано'}

📊 <b>Статус замовлення:</b>
├─ Було: {old_status_text}
└─ Стало: <b>{new_status_text}</b>

"""

        if payment_status_changed:
            message += "💰 <b>Статус оплати:</b> автоматично змінено на <b>ОПЛАЧЕНО</b>\n"
            message += "\n"

        message += f"""👤 <b>Клієнт:</b> {order.full_name}
📞 <b>Телефон:</b> {order.phone}
🏙️ <b>Місто:</b> {order.city}
💰 <b>Сума:</b> {order.total_sum} грн

🕐 <b>Час оновлення:</b> {timezone.now().strftime('%d.%m.%Y %H:%M')}

<i>Статус змінено автоматично через API Нової Пошти</i>"""

        # Складська дія: якщо вже продано — показуємо інформацію (без кнопки),
        # інакше — кнопку «продати зі складу», щоб не гортати до верхнього
        # повідомлення замовлення.
        reply_markup = None
        try:
            notifier = self.telegram_notifier
            sold_info = notifier._build_writeoff_status(order)
            if sold_info:
                # Вже продано/списано — додаємо перелік, кнопку не показуємо.
                message += sold_info
            else:
                storage_button = notifier._build_storage_action_button(order)
                if storage_button:
                    reply_markup = {"inline_keyboard": [[storage_button]]}
        except Exception:
            reply_markup = None

        try:
            self.telegram_notifier.send_admin_message(message, reply_markup=reply_markup)
            logger.debug(f"Admin notification sent for order {order.order_number}")
        except Exception as e:
            logger.error(f"Failed to send admin notification for order {order.order_number}: {e}")

        # Оновлюємо вихідне повідомлення замовлення: тепер статус done, тому
        # меню перебудується (зʼявиться складська кнопка / інфо «продано»).
        try:
            self.telegram_notifier.update_order_notification_message(order)
        except Exception as e:
            logger.error(
                f"Failed to refresh order message after delivery for {order.order_number}: {e}"
            )

    def _send_delivery_notification(self, order, shipment_status):
        """
        Отправляет специальное уведомление о получении посылки

        Args:
            order (Order): Заказ
            shipment_status (str): Статус посылки
        """
        if not order.user:
            logger.debug(f"Order {order.order_number}: no user, skipping delivery notification")
            return

        # Проверяем есть ли telegram_id у пользователя
        try:
            userprofile = getattr(order.user, 'userprofile', None)
            if userprofile is None:
                logger.debug(f"Order {order.order_number}: user has no profile")
                return

            telegram_id = getattr(userprofile, 'telegram_id', None)
            if not telegram_id:
                logger.debug(f"Order {order.order_number}: user has no telegram_id")
                return
        except (AttributeError, ObjectDoesNotExist) as e:
            logger.debug(
                f"Error accessing userprofile for order {order.order_number}: {e}"
            )
            return

        # Формируем сообщение о доставке
        message = self._format_delivery_message(order, shipment_status)

        # Отправляем личное сообщение пользователю
        try:
            self.telegram_notifier.send_personal_message(telegram_id, message)
            logger.info(f"Delivery notification sent to user for order {order.order_number}")
        except Exception as e:
            logger.error(f"Failed to send delivery notification for order {order.order_number}: {e}")

    def _send_status_notification(self, order, old_status, new_status):
        """
        Отправляет уведомление об изменении статуса в Telegram

        Args:
            order (Order): Заказ
            old_status (str): Старый статус
            new_status (str): Новый статус
        """
        if not order.user:
            logger.debug(f"Order {order.order_number}: no user, fallback to admin notification")
            self._send_admin_tracking_fallback(
                order,
                old_status=old_status,
                new_status=new_status,
                reason="no_user",
            )
            return

        # Проверяем есть ли telegram_id у пользователя
        try:
            userprofile = getattr(order.user, 'userprofile', None)
            if userprofile is None:
                logger.debug(f"Order {order.order_number}: user has no profile, fallback to admin")
                self._send_admin_tracking_fallback(
                    order,
                    old_status=old_status,
                    new_status=new_status,
                    reason="no_profile",
                )
                return

            telegram_id = getattr(userprofile, 'telegram_id', None)
            if not telegram_id:
                logger.debug(f"Order {order.order_number}: user has no telegram_id, fallback to admin")
                self._send_admin_tracking_fallback(
                    order,
                    old_status=old_status,
                    new_status=new_status,
                    reason="no_user_telegram_id",
                )
                return
        except (AttributeError, ObjectDoesNotExist) as e:
            logger.debug(
                f"Error accessing userprofile for order {order.order_number}: {e}"
            )
            self._send_admin_tracking_fallback(
                order,
                old_status=old_status,
                new_status=new_status,
                reason="profile_access_error",
            )
            return

        # Формируем сообщение
        message = self._format_status_message(order, old_status, new_status)

        # Отправляем личное сообщение пользователю
        try:
            sent = self.telegram_notifier.send_personal_message(telegram_id, message)
            if sent:
                logger.info(f"Status notification sent to user for order {order.order_number}")
            else:
                logger.warning(
                    f"Status notification not delivered to user for order {order.order_number}, "
                    f"fallback to admin"
                )
                self._send_admin_tracking_fallback(
                    order,
                    old_status=old_status,
                    new_status=new_status,
                    reason="user_send_failed",
                )
        except Exception as e:
            logger.error(f"Failed to send status notification for order {order.order_number}: {e}")
            self._send_admin_tracking_fallback(
                order,
                old_status=old_status,
                new_status=new_status,
                reason="user_send_exception",
            )

    def _send_admin_tracking_fallback(self, order, old_status, new_status, reason="unknown"):
        """
        Резервное уведомление админу, когда персональное уведомление клиенту недоступно.
        """
        if not self.telegram_notifier.is_configured():
            return

        customer = getattr(order, "full_name", "") or "Невідомо"
        phone = getattr(order, "phone", "") or "Невідомо"
        message = f"""📦 <b>РЕЗЕРВНЕ ОНОВЛЕННЯ СТАТУСУ ПОСИЛКИ</b>

🆔 <b>Замовлення:</b> #{order.order_number}
📋 <b>ТТН:</b> {order.tracking_number or 'Не вказано'}
👤 <b>Клієнт:</b> {customer}
📞 <b>Телефон:</b> {phone}

📊 <b>Статус змінено:</b>
├─ Було: {old_status or 'Невідомо'}
└─ Стало: <b>{new_status or 'Невідомо'}</b>

⚠️ <b>Причина fallback:</b> {reason}
🕐 <b>Час:</b> {timezone.now().strftime('%d.%m.%Y %H:%M')}"""

        try:
            self.telegram_notifier.send_admin_message(message)
            logger.info(
                f"Admin fallback notification sent for order {order.order_number} "
                f"(reason={reason})"
            )
        except Exception as e:
            logger.error(
                f"Failed to send admin fallback notification for order {order.order_number}: {e}"
            )

    def _format_delivery_message(self, order, shipment_status):
        """
        Форматирует красивое сообщение о получении посылки

        Args:
            order (Order): Заказ
            shipment_status (str): Статус посылки

        Returns:
            str: Отформатированное сообщение
        """
        message = f"""🎉 <b>ПОСИЛКА ОТРИМАНА!</b>

🆔 <b>Замовлення:</b> #{order.order_number}
📋 <b>ТТН:</b> {order.tracking_number}
📦 <b>Статус:</b> {shipment_status}

✅ <b>Ваше замовлення успішно доставлено!</b>
💰 <b>Сума:</b> {order.total_sum} грн

🕐 <b>Час отримання:</b> {timezone.now().strftime('%d.%m.%Y %H:%M')}

<i>Дякуємо за покупку! Сподіваємося, що товар вам сподобався.</i>

🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Допомога в Telegram</a>
• <a href="https://twocomms.shop/my-orders/">📋 Мої замовлення</a>"""

        return message

    def _format_status_message(self, order, old_status, new_status):
        """
        Форматирует сообщение об изменении статуса

        Args:
            order (Order): Заказ
            old_status (str): Старый статус
            new_status (str): Новый статус

        Returns:
            str: Отформатированное сообщение
        """
        message = f"""📦 <b>ОНОВЛЕННЯ СТАТУСУ ПОСИЛКИ</b>

🆔 <b>Замовлення:</b> #{order.order_number}
📋 <b>ТТН:</b> {order.tracking_number}

📊 <b>Статус змінено:</b>
├─ Було: {old_status or 'Невідомо'}
└─ Стало: <b>{new_status}</b>

🕐 <b>Час оновлення:</b> {timezone.now().strftime('%d.%m.%Y %H:%M')}

<i>Слідкуйте за оновленнями статусу вашої посилки!</i>

🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Допомога в Telegram</a>
• <a href="https://twocomms.shop/my-orders/">📋 Мої замовлення</a>"""

        return message

    def get_orders_with_tracking_queryset(self):
        """Return the single source of truth for scheduled tracking polls."""
        from storefront.models import UserAction

        now = timezone.now()
        purchase_order_ids = UserAction.objects.filter(
            action_type='purchase',
            order_id__isnull=False,
        ).values('order_id')
        base_orders = Order.objects.filter(
            tracking_number__isnull=False
        ).exclude(
            tracking_number=''
        ).exclude(
            status='cancelled'
        ).filter(
            created__gte=now - self.TRACKING_MAX_AGE
        ).filter(
            Q(tracking_terminal_at__isnull=True)
            & (Q(tracking_next_check_at__isnull=True) | Q(tracking_next_check_at__lte=now))
        )
        done_order = Q(status='done')
        done_received = done_order & Q(shipment_status__icontains='отримано')
        monobank_evidence = (
            Q(payment_provider__startswith='monobank')
            & Q(payment_invoice_id__isnull=False)
            & ~Q(payment_invoice_id='')
        )
        has_manual_preset = Q(payment_payload__has_key='manual_payment_preset')
        explicit_free = (
            has_manual_preset
            & Q(payment_payload__manual_payment_preset='free')
        )
        trusted_retry = (
            Q(source='web')
            | monobank_evidence
            | (has_manual_preset & ~Q(payment_payload__manual_payment_preset='free'))
        )
        retry_missing_purchase = (
            done_received
            & Q(payment_status__in=('paid', 'prepaid', 'partial'))
            & ~Q(pk__in=purchase_order_ids)
            & ~explicit_free
            & trusted_retry
        )
        return base_orders.filter(~done_order | retry_missing_purchase).order_by('created', 'pk')[:1000]

    def update_all_tracking_statuses(self):
        """
        Обновляет статусы всех заказов с ТТН

        Фильтрует заказы:
        - У которых есть tracking_number
        - Которые не в статусе 'done' или 'cancelled'

        Returns:
            dict: Статистика обновлений:
                - total_orders: общее количество заказов с ТТН
                - processed: обработано заказов
                - updated: обновлено статусов
                - errors: количество ошибок
        """
        logger.info("Starting update of all tracking statuses")

        orders_with_ttn = self.get_orders_with_tracking_queryset()

        close_old_connections()
        order_rows = list(
            orders_with_ttn.values('pk', 'order_number', 'tracking_number', 'phone')
        )
        total_orders = len(order_rows)
        updated_count = 0
        error_count = 0
        provider_error_count = 0
        row_error_count = 0
        application_error_count = 0
        processed_count = 0

        logger.info(f"Found {total_orders} orders with TTN to process")

        self._tracking_session = requests.Session()
        try:
            for offset in range(0, total_orders, self.TRACKING_BATCH_SIZE):
                batch_rows = order_rows[offset:offset + self.TRACKING_BATCH_SIZE]
                documents = [
                    {
                        'DocumentNumber': row['tracking_number'],
                        'Phone': row.get('phone') or '',
                    }
                    for row in batch_rows
                ]
                try:
                    if len(documents) == 1:
                        single = self.get_tracking_info(
                            documents[0]['DocumentNumber'],
                            phone=documents[0]['Phone'],
                        )
                        tracking_by_number = {
                            self._tracking_key(documents[0]['DocumentNumber']): single
                        } if single else {}
                    else:
                        tracking_by_number = self.get_tracking_info_batch(documents)
                except NovaPoshtaAPIError as exc:
                    error_count += len(batch_rows)
                    provider_error_count += len(batch_rows)
                    processed_count += len(batch_rows)
                    logger.warning(
                        "Nova Poshta tracking provider batch failed (%s rows): %s",
                        len(batch_rows),
                        exc,
                    )
                    self._defer_tracking_rows([row['pk'] for row in batch_rows])
                    continue
                except Exception as exc:
                    error_count += len(batch_rows)
                    application_error_count += len(batch_rows)
                    processed_count += len(batch_rows)
                    logger.exception(
                        "Nova Poshta tracking application batch failure (%s rows): %s",
                        len(batch_rows),
                        exc,
                    )
                    self._defer_tracking_rows([row['pk'] for row in batch_rows])
                    continue

                for row in batch_rows:
                    processed_count += 1
                    close_old_connections()
                    try:
                        order = Order.objects.get(pk=row['pk'])
                        key = self._tracking_key(row['tracking_number'])
                        tracking_info = tracking_by_number.get(key)
                        if not tracking_info:
                            error_count += 1
                            row_error_count += 1
                            self._defer_tracking_rows([order.pk])
                            logger.warning("Nova Poshta returned no result for TTN %s", row['tracking_number'])
                            continue
                        if self._apply_tracking_info_to_order(order, tracking_info):
                            updated_count += 1
                        if getattr(self, '_last_tracking_update_error', False):
                            error_count += 1
                            application_error_count += 1
                    except ObjectDoesNotExist:
                        logger.warning(f"Order pk={row['pk']} disappeared before tracking update")
                    except Exception as exc:
                        error_count += 1
                        application_error_count += 1
                        logger.exception("Error updating order pk=%s: %s", row['pk'], exc)
                    finally:
                        close_old_connections()
        finally:
            session = self._tracking_session
            self._tracking_session = None
            session.close()

        result = {
            'total_orders': total_orders,
            'processed': processed_count,
            'updated': updated_count,
            'errors': error_count,
            'provider_errors': provider_error_count,
            'row_errors': row_error_count,
            'application_errors': application_error_count,
        }

        logger.info(
            f"Finished updating tracking statuses: "
            f"{updated_count}/{total_orders} updated, {error_count} errors"
        )

        if error_count == 0:
            cache.set(self.LAST_UPDATE_CACHE_KEY, timezone.now(), timeout=None)
        else:
            logger.error(
                "Nova Poshta tracking heartbeat not updated because the batch had %s error(s)",
                error_count,
            )

        return result

    def _defer_tracking_rows(self, order_pks):
        """Back off a failed/partial batch without changing the last known status."""
        now = timezone.now()
        Order.objects.filter(pk__in=list(order_pks)).update(
            tracking_failure_count=F('tracking_failure_count') + 1,
            tracking_checked_at=now,
            tracking_next_check_at=now + self.ACTIVE_CHECK_INTERVAL,
        )

    @staticmethod
    def get_last_update_time():
        """
        Получает время последнего успешного обновления статусов

        Returns:
            datetime: Время последнего обновления или None
        """
        return cache.get(NovaPoshtaService.LAST_UPDATE_CACHE_KEY)

    @staticmethod
    def should_trigger_fallback_update():
        """
        Проверяет нужно ли запустить резервное обновление

        Если с момента последнего обновления прошло больше чем 
        NOVA_POSHTA_UPDATE_INTERVAL * FALLBACK_CHECK_MULTIPLIER минут,
        возвращает True (значит cron не работает)

        По умолчанию: 5 минут * 3 = 15 минут

        Returns:
            bool: True если нужно запустить резервное обновление
        """
        last_update = NovaPoshtaService.get_last_update_time()

        if last_update is None:
            # Первый запуск - нужно обновить
            logger.info("No previous updates found, fallback needed")
            return True

        # Получаем интервал обновления из настроек
        update_interval = getattr(settings, 'NOVA_POSHTA_UPDATE_INTERVAL', 5)
        try:
            update_interval = int(update_interval)
        except (TypeError, ValueError):
            logger.warning(
                f"Invalid NOVA_POSHTA_UPDATE_INTERVAL value: {update_interval}, using default 5 minutes"
            )
            update_interval = 5
        threshold_minutes = max(
            update_interval * NovaPoshtaService.FALLBACK_CHECK_MULTIPLIER,
            15  # минимум 15 минут
        )

        # Проверяем прошло ли больше порогового времени
        time_since_update = timezone.now() - last_update
        threshold = timedelta(minutes=threshold_minutes)

        needs_update = time_since_update > threshold

        if needs_update:
            logger.warning(
                f"Last update was {time_since_update.total_seconds() / 60:.1f} minutes ago "
                f"(threshold: {threshold_minutes} minutes), fallback needed"
            )

        return needs_update
