"""
Telegram уведомления для заказов
"""
import json
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests
from django.db import transaction
from django.utils import timezone

from orders.nova_poshta_documents import (
    TELEGRAM_CREATE_NP_WAYBILL_ACTION,
    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
    build_order_payment_snapshot,
)
from orders.status_management import get_telegram_status_action
from orders.telegram_status_links import build_order_action_url, build_order_status_action_url
# W3-1 (TD-015/TD-003): битый импорт удалён. Раньше здесь пытались
# импортировать send_telegram_notification_task из storefront.tasks, где её
# НЕТ (она в orders.tasks и с другой сигнатурой) → импорт всегда падал,
# async-ветки были мёртвым кодом, а отправка шла синхронно в request-потоке.
# Celery на прод не возвращаем (решение зафиксировано в docs/OPS.md);
# фоновая отправка живёт в orders/tasks.py (daemon-thread).
send_telegram_notification_task = None

logger = logging.getLogger(__name__)

_SEND_MESSAGE_MAX_ATTEMPTS = 3
_SEND_MESSAGE_RETRY_BASE_SECONDS = 0.1
_SEND_MESSAGE_RETRY_MAX_SECONDS = 0.25
_SEND_DOCUMENT_MAX_ATTEMPTS = 3
_SEND_DOCUMENT_RETRY_BASE_SECONDS = 0.2
_SEND_DOCUMENT_RETRY_MAX_SECONDS = 0.6


def _telegram_error_hint(payload):
    """Return a bounded, non-sensitive classification of a Bot API error."""
    if not isinstance(payload, dict):
        return "unknown"
    description = str(payload.get("description") or "").lower()
    if "inline keyboard" in description and "url" in description:
        return "invalid_inline_keyboard_url"
    if "chat not found" in description:
        return "chat_not_found"
    if "message is too long" in description:
        return "message_too_long"
    if "can't parse entities" in description or "parse entities" in description:
        return "invalid_parse_entities"
    if "bad request" in description:
        return "bad_request"
    try:
        error_code = int(payload.get("error_code") or 0)
    except (TypeError, ValueError):
        error_code = 0
    if error_code == 429:
        return "rate_limited"
    if error_code >= 500:
        return "telegram_server_error"
    return "redacted"


def _parse_chat_ids(raw_value):
    if not raw_value:
        return []
    parts = re.split(r"[;,\s]+", str(raw_value))
    return [part for part in (p.strip() for p in parts) if part]


def _normalize_order_admin_message_ref(chat_id, message_id):
    try:
        normalized_chat_id = int(chat_id)
        normalized_message_id = int(message_id)
    except (TypeError, ValueError):
        return None
    if not normalized_chat_id or not normalized_message_id:
        return None
    return {
        "chat_id": normalized_chat_id,
        "message_id": normalized_message_id,
    }


def _get_order_admin_message_refs(order):
    refs = []
    seen = set()

    primary_ref = _normalize_order_admin_message_ref(
        getattr(order, "admin_tg_chat_id", None),
        getattr(order, "admin_tg_message_id", None),
    )
    if primary_ref:
        refs.append(primary_ref)
        seen.add((primary_ref["chat_id"], primary_ref["message_id"]))

    raw_refs = getattr(order, "admin_tg_messages", None) or []
    if isinstance(raw_refs, list):
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, dict):
                continue
            normalized_ref = _normalize_order_admin_message_ref(
                raw_ref.get("chat_id"),
                raw_ref.get("message_id"),
            )
            if not normalized_ref:
                continue
            ref_key = (normalized_ref["chat_id"], normalized_ref["message_id"])
            if ref_key in seen:
                continue
            refs.append(normalized_ref)
            seen.add(ref_key)

    return refs


def _save_order_admin_message_refs(order, refs):
    normalized_refs = []
    seen = set()
    for ref in refs:
        normalized_ref = None
        if isinstance(ref, dict):
            normalized_ref = _normalize_order_admin_message_ref(
                ref.get("chat_id"),
                ref.get("message_id"),
            )
        elif isinstance(ref, (list, tuple)) and len(ref) >= 2:
            normalized_ref = _normalize_order_admin_message_ref(ref[0], ref[1])
        if not normalized_ref:
            continue
        ref_key = (normalized_ref["chat_id"], normalized_ref["message_id"])
        if ref_key in seen:
            continue
        normalized_refs.append(normalized_ref)
        seen.add(ref_key)

    if normalized_refs:
        order.admin_tg_chat_id = normalized_refs[0]["chat_id"]
        order.admin_tg_message_id = normalized_refs[0]["message_id"]
        order.admin_tg_messages = normalized_refs
    else:
        order.admin_tg_chat_id = None
        order.admin_tg_message_id = None
        order.admin_tg_messages = None

    if hasattr(order, "save"):
        order.save(update_fields=["admin_tg_chat_id", "admin_tg_message_id", "admin_tg_messages"])
    return normalized_refs


class TelegramNotifier:
    """Класс для отправки уведомлений в Telegram"""

    def __init__(
        self,
        *,
        bot_token=None,
        chat_id=None,
        admin_id=None,
        bot_token_env="TELEGRAM_BOT_TOKEN",
        chat_id_env="TELEGRAM_CHAT_ID",
        admin_id_env="TELEGRAM_ADMIN_ID",
        # W3-1: False по умолчанию — Celery-воркера на проде нет,
        # «async» через .delay() публиковал в мёртвую очередь.
        async_enabled=False,
    ):
        self.bot_token = bot_token if bot_token is not None else os.environ.get(bot_token_env)
        self.chat_id = chat_id if chat_id is not None else os.environ.get(chat_id_env)
        self.admin_id = admin_id if admin_id is not None else os.environ.get(admin_id_env)
        self.chat_ids = _parse_chat_ids(self.chat_id)
        self.admin_ids = _parse_chat_ids(self.admin_id)
        self.async_enabled = bool(async_enabled)

    def is_configured(self):
        """Проверяет, настроен ли бот"""
        return bool(self.bot_token and (self.chat_ids or self.admin_ids))

    def _resolve_targets(self, admin=True):
        if admin:
            return self.admin_ids or self.chat_ids
        return self.chat_ids or self.admin_ids

    def _post_json(self, method, *, data=None, files=None, timeout=10):
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        response = requests.post(url, data=data, files=files, timeout=timeout)
        try:
            return response.json()
        except ValueError:
            return None

    def _post(self, method, *, data=None, files=None, timeout=10):
        payload = self._post_json(method, data=data, files=files, timeout=timeout)
        return bool(payload and payload.get("ok"))

    def _post_send_message_json(self, *, data, timeout, target_index, target_count):
        """POST one text message with bounded retries for transient failures."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for attempt in range(1, _SEND_MESSAGE_MAX_ATTEMPTS + 1):
            status_code = None
            try:
                response = requests.post(url, data=data, timeout=timeout)
                status_code = response.status_code
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _SEND_MESSAGE_MAX_ATTEMPTS:
                    logger.warning(
                        "telegram_send_message retry_exhausted attempt=%s target_index=%s "
                        "target_count=%s reason=exception exception_class=%s",
                        attempt,
                        target_index,
                        target_count,
                        type(exc).__name__,
                    )
                    return None
                logger.warning(
                    "telegram_send_message retry_scheduled attempt=%s target_index=%s "
                    "target_count=%s reason=exception exception_class=%s",
                    attempt,
                    target_index,
                    target_count,
                    type(exc).__name__,
                )
                self._sleep_before_send_message_retry(attempt)
                continue
            except Exception as exc:
                logger.warning(
                    "telegram_send_message delivery_failed attempt=%s target_index=%s "
                    "target_count=%s reason=exception exception_class=%s",
                    attempt,
                    target_index,
                    target_count,
                    type(exc).__name__,
                )
                return None

            if status_code == 429 or status_code >= 500:
                if attempt == _SEND_MESSAGE_MAX_ATTEMPTS:
                    logger.warning(
                        "telegram_send_message retry_exhausted attempt=%s target_index=%s "
                        "target_count=%s reason=http_status status=%s",
                        attempt,
                        target_index,
                        target_count,
                        status_code,
                    )
                    return None
                logger.warning(
                    "telegram_send_message retry_scheduled attempt=%s target_index=%s "
                    "target_count=%s reason=http_status status=%s",
                    attempt,
                    target_index,
                    target_count,
                    status_code,
                )
                self._sleep_before_send_message_retry(attempt)
                continue

            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "telegram_send_message api_rejected attempt=%s target_index=%s "
                    "target_count=%s status=%s reason=invalid_json",
                    attempt,
                    target_index,
                    target_count,
                    status_code,
                )
                return None

            if payload and payload.get("ok"):
                if attempt > 1:
                    logger.info(
                        "telegram_send_message retry_recovered attempt=%s target_index=%s "
                        "target_count=%s",
                        attempt,
                        target_index,
                        target_count,
                    )
                return payload

            logger.warning(
                "telegram_send_message api_rejected attempt=%s target_index=%s "
                "target_count=%s status=%s error_code=%s description_hint=%s",
                attempt,
                target_index,
                target_count,
                status_code,
                payload.get("error_code") if isinstance(payload, dict) else None,
                _telegram_error_hint(payload),
            )
            return None
        return None

    @staticmethod
    def _sleep_before_send_message_retry(attempt):
        delay = min(
            _SEND_MESSAGE_RETRY_BASE_SECONDS * attempt,
            _SEND_MESSAGE_RETRY_MAX_SECONDS,
        )
        time.sleep(delay)

    def _post_send_document(self, *, file_path, filename, data, parse_mode, target_index, target_count):
        """Send one document with bounded retries for transient Telegram failures."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        request_data = dict(data)
        request_data["parse_mode"] = parse_mode
        for attempt in range(1, _SEND_DOCUMENT_MAX_ATTEMPTS + 1):
            status_code = None
            try:
                with open(file_path, "rb") as file_obj:
                    files = {"document": (filename or Path(file_path).name, file_obj)}
                    response = requests.post(url, data=request_data, files=files, timeout=30)
                status_code = response.status_code
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == _SEND_DOCUMENT_MAX_ATTEMPTS:
                    logger.warning(
                        "telegram_send_document retry_exhausted attempt=%s target_index=%s "
                        "target_count=%s reason=exception exception_class=%s",
                        attempt,
                        target_index,
                        target_count,
                        type(exc).__name__,
                    )
                    return None
                logger.warning(
                    "telegram_send_document retry_scheduled attempt=%s target_index=%s "
                    "target_count=%s reason=exception exception_class=%s",
                    attempt,
                    target_index,
                    target_count,
                    type(exc).__name__,
                )
                delay = min(_SEND_DOCUMENT_RETRY_BASE_SECONDS * attempt, _SEND_DOCUMENT_RETRY_MAX_SECONDS)
                time.sleep(delay)
                continue
            except Exception as exc:
                logger.warning(
                    "telegram_send_document delivery_failed attempt=%s target_index=%s "
                    "target_count=%s exception_class=%s",
                    attempt,
                    target_index,
                    target_count,
                    type(exc).__name__,
                )
                return None

            if status_code == 429 or status_code >= 500:
                if attempt == _SEND_DOCUMENT_MAX_ATTEMPTS:
                    logger.warning(
                        "telegram_send_document retry_exhausted attempt=%s target_index=%s "
                        "target_count=%s reason=http_status status=%s",
                        attempt,
                        target_index,
                        target_count,
                        status_code,
                    )
                    return None
                logger.warning(
                    "telegram_send_document retry_scheduled attempt=%s target_index=%s "
                    "target_count=%s reason=http_status status=%s",
                    attempt,
                    target_index,
                    target_count,
                    status_code,
                )
                delay = min(_SEND_DOCUMENT_RETRY_BASE_SECONDS * attempt, _SEND_DOCUMENT_RETRY_MAX_SECONDS)
                time.sleep(delay)
                continue

            try:
                payload = response.json()
            except ValueError:
                logger.warning(
                    "telegram_send_document api_rejected attempt=%s target_index=%s "
                    "target_count=%s status=%s reason=invalid_json",
                    attempt,
                    target_index,
                    target_count,
                    status_code,
                )
                return None

            if payload and payload.get("ok"):
                if attempt > 1:
                    logger.info(
                        "telegram_send_document retry_recovered attempt=%s target_index=%s "
                        "target_count=%s",
                        attempt,
                        target_index,
                        target_count,
                    )
                return payload

            logger.warning(
                "telegram_send_document api_rejected attempt=%s target_index=%s "
                "target_count=%s status=%s",
                attempt,
                target_index,
                target_count,
                status_code,
            )
            return None
        return None

    def send_message(self, message, parse_mode='HTML', reply_markup=None, return_results=False):
        """Отправляет сообщение в Telegram админу"""
        if not self.is_configured():
            logger.warning("telegram_send_message not_configured")
            return False

        # Используем админ ID, если он доступен, иначе chat_id
        target_ids = self._resolve_targets(admin=True)
        if not target_ids:
            return [] if return_results else False

        # W3-1: async-ветка удалена — она зависела от битого импорта и
        # никогда не выполнялась. Отправка синхронная; фоновость
        # обеспечивает вызывающий код (orders/tasks.py, daemon-thread).
        sent_results = []
        delivered_count = 0
        target_count = len(target_ids)
        for target_index, target_id in enumerate(target_ids, start=1):
            try:
                data = {
                    'chat_id': target_id,
                    'text': message,
                    'parse_mode': parse_mode
                }
                if reply_markup is not None:
                    data['reply_markup'] = json.dumps(reply_markup, ensure_ascii=False)
                payload = self._post_send_message_json(
                    data=data,
                    timeout=10,
                    target_index=target_index,
                    target_count=target_count,
                )
                if payload and payload.get("ok"):
                    delivered_count += 1
                    result = payload.get("result") or {}
                    if return_results:
                        sent_results.append(result)
            except Exception as exc:
                logger.warning(
                    "telegram_send_message delivery_failed target_index=%s target_count=%s "
                    "exception_class=%s",
                    target_index,
                    target_count,
                    type(exc).__name__,
                )

        if 0 < delivered_count < target_count:
            logger.warning(
                "telegram_send_message partial_delivery delivered_count=%s target_count=%s",
                delivered_count,
                target_count,
            )
        return sent_results if return_results else delivered_count > 0

    def send_admin_message(self, message, parse_mode='HTML', reply_markup=None):
        """
        Псевдоним для send_message для единообразия API.
        Отправляет сообщение администратору.

        Args:
            message (str): Текст сообщения
            parse_mode (str): Режим парсинга ('HTML' или 'Markdown')

        Returns:
            bool: True если сообщение отправлено успешно
        """
        return self.send_message(message, parse_mode, reply_markup=reply_markup)

    def send_admin_document(self, file_path, caption, filename=None, parse_mode='HTML'):
        """Отправляет документ админу в Telegram."""
        if not self.is_configured():
            logger.warning("telegram_send_document not_configured")
            return False

        target_ids = self._resolve_targets(admin=True)
        if not target_ids:
            logger.warning("telegram_send_document no_targets")
            return False
        delivered_count = 0
        target_count = len(target_ids)
        for target_index, target_id in enumerate(target_ids, start=1):
            payload = self._post_send_document(
                file_path=file_path,
                filename=filename,
                data={"chat_id": target_id, "caption": caption},
                parse_mode=parse_mode,
                target_index=target_index,
                target_count=target_count,
            )
            if payload and payload.get("ok"):
                delivered_count += 1
        if 0 < delivered_count < target_count:
            logger.warning(
                "telegram_send_document partial_delivery delivered_count=%s target_count=%s",
                delivered_count,
                target_count,
            )
        return delivered_count > 0

    def send_admin_photo(self, file_path, caption="", parse_mode='HTML'):
        """Отправляет фото админу."""
        if not self.is_configured():
            return False

        target_ids = self._resolve_targets(admin=True)
        if not target_ids:
            return False
        try:
            success = False
            for target_id in target_ids:
                with open(file_path, 'rb') as file_obj:
                    files = {'photo': (Path(file_path).name, file_obj)}
                    data = {
                        'chat_id': target_id,
                        'caption': caption,
                        'parse_mode': parse_mode,
                    }
                    success = self._post("sendPhoto", data=data, files=files, timeout=30) or success
            return success
        except Exception as e:
            print(f"Ошибка при отправке фото в Telegram: {e}")
            return False

    def send_admin_media_group(self, file_paths, captions=None, parse_mode='HTML'):
        """Отправляет группу изображений админу через sendMediaGroup."""
        if not self.is_configured():
            return False

        file_paths = [str(path) for path in (file_paths or []) if path]
        if not file_paths:
            return False

        target_ids = self._resolve_targets(admin=True)
        if not target_ids:
            return False

        captions = captions or []
        success = False
        for target_id in target_ids:
            files = {}
            media = []
            handles = []
            try:
                for index, file_path in enumerate(file_paths):
                    handle = open(file_path, 'rb')
                    handles.append(handle)
                    attach_name = f"file{index}"
                    files[attach_name] = (Path(file_path).name, handle)
                    item = {
                        'type': 'photo',
                        'media': f'attach://{attach_name}',
                    }
                    if index < len(captions) and captions[index]:
                        item['caption'] = captions[index]
                        item['parse_mode'] = parse_mode
                    media.append(item)
                data = {
                    'chat_id': target_id,
                    'media': json.dumps(media, ensure_ascii=False),
                }
                success = self._post("sendMediaGroup", data=data, files=files, timeout=45) or success
            except Exception as e:
                print(f"Ошибка при отправке media group в Telegram: {e}")
            finally:
                for handle in handles:
                    handle.close()
        return success

    def send_admin_document_group(self, file_paths, captions=None, parse_mode='HTML'):
        """Отправляет группу документов админу одним альбомом (sendMediaGroup type=document).

        Telegram Bot API позволяет смешать до 10 документов в одном sendMediaGroup,
        что превращает «7 отдельных карточек» в одну компактную пачку.
        """
        if not self.is_configured():
            return False

        file_paths = [str(path) for path in (file_paths or []) if path]
        if not file_paths:
            return False

        target_ids = self._resolve_targets(admin=True)
        if not target_ids:
            return False

        captions = captions or []
        success = False
        for target_id in target_ids:
            files = {}
            media = []
            handles = []
            try:
                for index, file_path in enumerate(file_paths):
                    handle = open(file_path, 'rb')
                    handles.append(handle)
                    attach_name = f"file{index}"
                    files[attach_name] = (Path(file_path).name, handle)
                    item = {
                        'type': 'document',
                        'media': f'attach://{attach_name}',
                    }
                    if index < len(captions) and captions[index]:
                        item['caption'] = captions[index]
                        item['parse_mode'] = parse_mode
                    media.append(item)
                data = {
                    'chat_id': target_id,
                    'media': json.dumps(media, ensure_ascii=False),
                }
                success = self._post("sendMediaGroup", data=data, files=files, timeout=60) or success
            except Exception as e:
                print(f"Ошибка при отправке document group в Telegram: {e}")
            finally:
                for handle in handles:
                    handle.close()
        return success


    def send_personal_message(self, telegram_id, message, parse_mode='HTML'):
        """Отправляет личное сообщение пользователю по telegram_id"""
        if not self.bot_token:
            logger.warning("telegram_send_message not_configured channel=personal")
            return False

        if not telegram_id:
            logger.warning("telegram_send_message invalid_target channel=personal")
            return False

        # W3-1: мёртвая async-ветка удалена (см. комментарий у импорта).
        data = {
            'chat_id': telegram_id,
            'text': message,
            'parse_mode': parse_mode,
        }
        payload = self._post_send_message_json(
            data=data,
            timeout=10,
            target_index=1,
            target_count=1,
        )
        return bool(payload and payload.get("ok"))

    def _format_payment_info(self, order):
        """
        Форматирует информацию об оплате в зависимости от pay_type и payment_status

        Args:
            order: Объект заказа

        Returns:
            str: Отформатированная информация об оплате
        """
        payment_info = "│  💳 ОПЛАТА:\n"
        snapshot = build_order_payment_snapshot(order)
        pay_type = snapshot['pay_type']
        payment_status = snapshot['payment_status']
        gross = self._fmt_amount(snapshot['gross_total_value'])
        discount = self._fmt_amount(snapshot['discount_amount_value'])
        payable = self._fmt_amount(snapshot['payable_total_value'])
        prepayment = self._fmt_amount(snapshot['prepayment_amount_value'])
        remaining = self._fmt_amount(snapshot['remaining_amount_value'])

        # Определяем тип оплаты
        if pay_type == 'online_full' or pay_type == 'full':
            payment_info += "│     Тип: Онлайн оплата (повна сума)\n"

            if payment_status == 'paid':
                payment_info += "│     ✅ ОПЛАЧЕНО ПОВНІСТЮ\n"
                payment_info += f"│     💰 Сплачено: {payable} грн\n"
            elif payment_status == 'checking':
                payment_info += "│     ⏳ РАХУНОК СТВОРЕНО, ОЧІКУЄМО ОПЛАТУ\n"
                payment_info += f"│     💰 До сплати: {payable} грн\n"
            else:
                payment_info += "│     ⏳ Очікується оплата\n"
                payment_info += f"│     💰 До сплати: {payable} грн\n"

        elif pay_type == 'prepay_200' or pay_type == 'partial':
            payment_info += "│     Тип: Передплата 200 грн\n"

            if payment_status == 'prepaid' or payment_status == 'partial':
                payment_info += f"│     ✅ ПЕРЕДПЛАТА ВНЕСЕНА: {prepayment} грн\n"
                payment_info += f"│     📦 Залишок (при отриманні): {remaining} грн\n"
                payment_info += f"│     💰 До сплати за замовлення: {payable} грн\n"
            elif payment_status == 'paid':
                payment_info += f"│     ✅ ОПЛАЧЕНО ПОВНІСТЮ: {payable} грн\n"
                payment_info += "│     (Передплата + залишок при отриманні)\n"
            else:
                payment_info += f"│     ⏳ Очікується передплата: {prepayment} грн\n"
                payment_info += f"│     📦 Залишок (при отриманні): {remaining} грн\n"
                payment_info += f"│     💰 До сплати за замовлення: {payable} грн\n"

        elif pay_type == 'cod':
            payment_info += "│     Тип: Оплата при отриманні\n"

            if payment_status == 'paid':
                payment_info += "│     ✅ ОПЛАЧЕНО\n"
                payment_info += f"│     💰 Сплачено: {payable} грн\n"
            else:
                payment_info += "│     📦 Накладений платіж\n"
                payment_info += f"│     💰 До сплати: {payable} грн\n"
        else:
            # Fallback для неизвестных типов
            payment_info += f"│     Тип: {pay_type}\n"
            payment_info += f"│     Статус: {order.get_payment_status_display()}\n"
            payment_info += f"│     💰 До сплати: {payable} грн\n"

        if snapshot['discount_amount_value'] > 0:
            payment_info += f"│     🧾 Товари: {gross} грн · знижка: -{discount} грн\n"

        return payment_info

    def format_order_message(self, order):
        """Форматирует HTML сообщение о заказе с поддержкой Telegram"""
        # Основная информация о заказе
        order_header = f"🆕 <b>НОВЕ ЗАМОВЛЕННЯ #{order.order_number}</b>\n"

        # Подсчитываем товары
        total_items = 0
        subtotal = Decimal('0.00')

        for item in order.items.all():
            total_items += item.qty
            subtotal += item.line_total

        # Форматируем информацию об оплате
        payment_info = self._format_payment_info(order)

        # Создаем единый блок с всей информацией
        full_block = f"""
<pre language="text">
┌─────────────────────────────────────────┐
│  🆕 ЗАКАЗ #{order.order_number}                   │
├─────────────────────────────────────────┤
│  👤 КЛИЕНТ:
│     Имя: {order.full_name}
│     Телефон: {order.phone}
│     Город: {order.city}
│     НП: {order.np_office}
├─────────────────────────────────────────┤
│  📋 ДЕТАЛИ ЗАКАЗА:
│     Статус оплаты: {order.get_payment_status_display()}
│     Статус заказа: {order.get_status_display()}
│     Время создания: {order.created.strftime('%d.%m.%Y %H:%M')}
"""

        if getattr(order, "tracking_number", None):
            full_block += f"│     ТТН: {order.tracking_number}\n"

        full_block += f"""
├─────────────────────────────────────────┤
{payment_info}
├─────────────────────────────────────────┤
│  📦 ТОВАРЫ В ЗАКАЗЕ ({order.items.count()} позиций):
"""

        # Добавляем товары
        for i, item in enumerate(order.items.all(), 1):
            full_block += f"│     {i}. {item.title}\n"

            # Добавляем детали товара каждая на новой строке с └
            if item.size:
                full_block += f"│        └ Размер: {item.size}\n"
            if item.fit_label:
                full_block += f"│        └ Посадка: {item.fit_label}\n"
            for option_label in getattr(item, "generic_option_labels", []) or []:
                full_block += f"│        └ {option_label}\n"
            if item.qty:
                full_block += f"│        └ Количество: {item.qty}\n"
            if item.color_variant:
                full_block += f"│        └ Цвет: {item.color_variant.color.name}\n"
            if item.unit_price:
                full_block += f"│        └ Цена: {item.unit_price} грн\n"

            if i < order.items.count():
                full_block += "│     ───────────────────────────────────\n"

        # Добавляем итоговую информацию
        full_block += f"├─────────────────────────────────────────┤\n"
        full_block += f"│  📊 ИТОГОВАЯ ИНФОРМАЦИЯ:\n"
        full_block += f"│     Всего товаров: {total_items} шт.\n"
        full_block += f"│     Сумма товаров: {subtotal} грн\n"

        snapshot = build_order_payment_snapshot(order)
        gross = self._fmt_amount(snapshot['gross_total_value'])
        discount = self._fmt_amount(snapshot['discount_amount_value'])
        payable = self._fmt_amount(snapshot['payable_total_value'])

        if snapshot['discount_amount_value'] > 0:
            if order.promo_code:
                full_block += f"│     Промокод: {order.promo_code.code}\n"
            full_block += f"│     Знижка: -{discount} грн\n"

        full_block += f"├─────────────────────────────────────────┤\n"
        full_block += f"│  💰 Сума товарів: {gross} грн\n"
        full_block += f"│  💳 ДО СПЛАТИ: {payable} грн\n"
        full_block += f"└─────────────────────────────────────────┘"
        full_block += "</pre>"

        # Добавляем ссылки
        links = """

🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Допомога в Telegram</a>
• <a href="https://twocomms.shop/my-orders/">📋 Мої замовлення</a>"""

        # Собираем полное сообщение
        message = f"{order_header}\n{full_block}\n{links}"

        prints_block = self._build_prints_summary(order)
        if prints_block:
            message += prints_block

        writeoff_block = self._build_writeoff_status(order)
        if writeoff_block:
            message += writeoff_block

        return message

    def _build_writeoff_status(self, order):
        """Блок «✅ Списано зі складу» — показує, що по замовленню вже зроблено списання.

        Безпечно: якщо warehouse недоступний або списання не було — повертає ''.
        Саме цей блок змінює вихідне повідомлення після успішного списання.
        """
        try:
            from warehouse.models import WriteOffRequest
        except Exception:
            return ""
        try:
            wo = (
                order.warehouse_write_off_requests.filter(
                    status=WriteOffRequest.STATUS_COMPLETED
                )
                .order_by("-completed_at")
                .first()
            )
            if not wo:
                return ""
            when = ""
            if wo.completed_at:
                when = timezone.localtime(wo.completed_at).strftime("%d.%m.%Y %H:%M")
            lines = []
            for mv in wo.movements.all():
                target = mv.target
                name = str(target) if target is not None else "—"
                lines.append(f"• {abs(mv.delta)}× {name}")
            header = f"\n\n✅ <b>Списано зі складу</b>"
            if when:
                header += f" · {when}"
            header += "\n"
            if lines:
                return header + "\n".join(lines)
            return header + "—"
        except Exception:
            return ""

    def _build_prints_summary(self, order):
        """Блок «🎨 Принти для списання» — перелік прив'язаних принтів за позиціями.

        Безпечно: якщо warehouse недоступний або принтів немає — повертає ''.
        Допомагає оператору одразу бачити, що під цей одяг є принти,
        які треба буде списати (наприклад лого на грудь + дизайн на спину).
        """
        try:
            from warehouse.services.matching import find_prints_for_order_item
        except Exception:
            return ""
        try:
            lines = []
            total = 0
            for item in order.items.all():
                prints = find_prints_for_order_item(item)
                if not prints:
                    continue
                parts = []
                for pr in prints:
                    placement = pr.get_placement_display() if getattr(pr, "placement", "") else ""
                    parts.append(f"{pr.name} ({placement})" if placement else pr.name)
                    total += 1
                title = getattr(item, "title", "") or "—"
                lines.append(f"• <b>{title}</b>: " + ", ".join(parts))
            if not lines:
                return ""
            header = f"\n\n🎨 <b>Принти для списання</b> ({total}):\n"
            return header + "\n".join(lines)
        except Exception:
            return ""

    def _build_storage_action_button(self, order):
        """Єдина складська кнопка: «продати зі складу» АБО «відмінити продаж».

        - Якщо є завершена (не скасована) продажа → «↩️ Відмінити продаж».
        - Інакше → «📦 Списати/продати зі складу» (відкриває сторінку продажу).
        Повертає dict однієї inline-кнопки або None.
        """
        try:
            from warehouse.models import WriteOffRequest
            from warehouse.services.order_links import (
                build_storage_cancel_sale_url,
                build_storage_writeoff_url,
            )
        except Exception:
            return None
        try:
            has_completed = order.warehouse_write_off_requests.filter(
                status=WriteOffRequest.STATUS_COMPLETED
            ).exists()
        except Exception:
            has_completed = False

        if has_completed:
            try:
                cancel_url = build_storage_cancel_sale_url(order)
            except Exception:
                cancel_url = None
            if cancel_url:
                return {"text": "↩️ Відмінити продаж", "url": cancel_url}
            return None

        try:
            writeoff_url = build_storage_writeoff_url(order)
        except Exception:
            return None
        return {"text": "📦 Списати/продати зі складу", "url": writeoff_url}

    # Зворотна сумісність зі старою назвою (використовується в тестах/іншому коді).
    def _build_storage_writeoff_button(self, order):
        return self._build_storage_action_button(order)

    def _build_order_management_reply_markup(self, order):
        if not getattr(order, "pk", None):
            return None

        status = getattr(order, "status", "")
        storage_button = self._build_storage_action_button(order)

        # Скасоване замовлення — жодних дій.
        if status == "cancelled":
            return None

        # Доставлене/отримане замовлення: лишаємо лише складську дію
        # (продати зі складу / відмінити продаж), щоб не гортати вгору.
        if status == "done":
            if storage_button:
                return {"inline_keyboard": [[storage_button]]}
            return None

        if getattr(order, "nova_poshta_document_ref", None):
            try:
                delete_waybill_url = build_order_action_url(
                    order,
                    TELEGRAM_DELETE_NP_WAYBILL_ACTION,
                    route_name="telegram_order_np_waybill_action",
                    token_scope=getattr(order, "nova_poshta_document_ref", "") or "",
                )
            except Exception:
                delete_waybill_url = None

            keyboard_rows = []
            if delete_waybill_url:
                keyboard_rows.append([{"text": "🗑 Видалити ТТН НП", "url": delete_waybill_url}])
            if storage_button:
                keyboard_rows.append([storage_button])
            return {"inline_keyboard": keyboard_rows} if keyboard_rows else None

        if getattr(order, "tracking_number", None):
            if storage_button:
                return {"inline_keyboard": [[storage_button]]}
            return None

        action = get_telegram_status_action('ship')
        if not action:
            if storage_button:
                return {"inline_keyboard": [[storage_button]]}
            return None

        try:
            ship_url = build_order_status_action_url(order, 'ship')
            create_waybill_url = build_order_action_url(
                order,
                TELEGRAM_CREATE_NP_WAYBILL_ACTION,
                route_name="telegram_order_np_waybill_action",
            )
        except Exception:
            if storage_button:
                return {"inline_keyboard": [[storage_button]]}
            return None

        keyboard_rows = [
            [{"text": "📦 Створити ТТН НП", "url": create_waybill_url}],
            [{"text": action["button_text"], "url": ship_url}],
        ]
        if storage_button:
            keyboard_rows.append([storage_button])
        return {"inline_keyboard": keyboard_rows}

    def send_new_order_notification(self, order):
        """Отправляет уведомление о новом заказе"""
        if not self.is_configured():
            return False

        message = self.format_order_message(order)
        reply_markup = self._build_order_management_reply_markup(order)
        sent_results = self.send_message(
            message,
            reply_markup=reply_markup,
            return_results=True,
        )
        if sent_results and getattr(order, "pk", None):
            refs = []
            for result in sent_results:
                chat = result.get("chat") or {}
                refs.append((chat.get("id"), result.get("message_id")))
            _save_order_admin_message_refs(order, refs)
            try:
                from orders.models import Order

                with transaction.atomic():
                    current = Order.objects.select_for_update().get(pk=order.pk)
                    payload = current.payment_payload if isinstance(current.payment_payload, dict) else {}
                    notifications = payload.setdefault('telegram_notifications', {})
                    notifications['order_notification_sent'] = True
                    notifications['order_notification_sent_at'] = timezone.now().isoformat()
                    notifications['order_notification_status'] = current.payment_status
                    current.payment_payload = payload
                    current.save(update_fields=['payment_payload'])
                    order.payment_payload = payload
            except Exception:
                logger.exception('Failed to persist Telegram delivery marker for order %s', order.pk)
        return bool(sent_results)

    def send_payment_attempt_notification(self, attempt):
        """Notify staff that a checkout invoice was opened, before an order exists."""
        if not self.is_configured():
            return False
        gross = self._fmt_amount(getattr(attempt, 'gross_amount', 0))
        discount = self._fmt_amount(getattr(attempt, 'discount_amount', 0))
        amount = self._fmt_amount(getattr(attempt, 'payable_amount', 0))
        payment_amount = self._fmt_amount(getattr(attempt, 'payment_amount', 0))
        pay_label = 'Передплата 200 грн' if getattr(attempt, 'pay_type', '') == 'prepay_200' else 'Повна онлайн-оплата'
        snapshot = getattr(attempt, 'cart_snapshot', None) or {}
        item_lines = []
        for item in (snapshot.get('cart') or [])[:5] if isinstance(snapshot, dict) else []:
            title = str(item.get('title') or 'Товар').strip()
            qty = int(item.get('qty') or 1)
            line_total = self._fmt_amount(item.get('line_total', 0))
            item_lines.append(f"• {title} × {qty} · {line_total} грн")
        if item_lines:
            item_block = "\n" + "\n".join(item_lines)
        else:
            item_block = "\n• Позиції уточнюються"
        message = (
            f"💳 <b>Спроба оплати #{attempt.reference}</b>\n"
            f"👤 {attempt.full_name} · {attempt.phone}\n"
            f"📦 {attempt.city}, {attempt.np_office}\n"
            f"🛍 <b>Товар:</b>{item_block}\n"
            f"💰 Товари: {gross} грн · знижка: -{discount} грн\n"
            f"💰 До сплати: <b>{amount} грн</b> · зараз: <b>{payment_amount} грн</b>\n"
            f"🔐 {pay_label}\n"
            f"⏳ Статус: очікуємо підтвердження Monobank\n"
            f"🧾 Invoice: <code>{attempt.monobank_invoice_id or 'створюється'}</code>"
        )
        return bool(self.send_message(message, return_results=True))

    def update_order_notification_message(self, order, *, clear_actions=False):
        refs = _get_order_admin_message_refs(order)
        if not refs or not self.bot_token:
            return False

        message = self.format_order_message(order)
        reply_markup = {"inline_keyboard": []} if clear_actions else self._build_order_management_reply_markup(order)
        success = False
        for ref in refs:
            payload = {
                "chat_id": ref["chat_id"],
                "message_id": ref["message_id"],
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            result = self._post_json("editMessageText", data=payload, timeout=10)
            success = bool(result and result.get("ok")) or success
        return success

    @staticmethod
    def _fmt_amount(value):
        """Гривні без зайвих нулів: 880.00 → '880', 760.50 → '760.5'."""
        try:
            dec = Decimal(value or 0)
        except (TypeError, InvalidOperation):
            return str(value)
        normalized = dec.normalize()
        text = format(normalized, 'f')
        return text

    def format_order_edit_message(self, order, diff, changed_by=None):
        """Формує HTML-текст сповіщення про редагування замовлення з diff."""
        lines = [f"✏️ <b>Замовлення #{order.order_number} відредаговано</b>"]
        if changed_by:
            lines.append(f"👤 Змінив: {changed_by}")
        lines.append("")

        items = diff.get('items') or {}
        removed = items.get('removed') or []
        added = items.get('added') or []
        changed = items.get('changed') or []

        for it in removed:
            lines.append(f"➖ <b>Видалено:</b> {it['label']} ×{it['qty']}")
        for it in added:
            lines.append(f"➕ <b>Додано:</b> {it['label']} ×{it['qty']}")
        for it in changed:
            details = []
            if it['old_qty'] != it['new_qty']:
                details.append(f"к-сть {it['old_qty']} → {it['new_qty']}")
            if it['old_price'] != it['new_price']:
                details.append(
                    f"ціна {self._fmt_amount(it['old_price'])} → {self._fmt_amount(it['new_price'])} грн"
                )
            lines.append(f"🔁 <b>{it['label']}:</b> " + ", ".join(details))

        total = diff.get('total')
        if total:
            delta = Decimal(total['delta'])
            sign = "+" if delta > 0 else ""
            lines.append("")
            lines.append(
                f"💰 Сума: {self._fmt_amount(total['old'])} → "
                f"<b>{self._fmt_amount(total['new'])} грн</b> "
                f"({sign}{self._fmt_amount(delta)} грн)"
            )

        delivery = diff.get('delivery')
        if delivery:
            lines.append(f"📍 Доставка: {delivery['old'] or '—'} → <b>{delivery['new'] or '—'}</b>")

        customer = diff.get('customer')
        if customer:
            old_name, old_phone = customer['old']
            new_name, new_phone = customer['new']
            lines.append(f"🧑 Клієнт: {old_name} {old_phone} → <b>{new_name} {new_phone}</b>")

        return "\n".join(lines)

    def send_order_edit_notification(self, order, diff, changed_by=None):
        """Шле адмінам нове повідомлення з переліком змін замовлення.

        Якщо змін немає (``diff['has_changes']`` хибне) — нічого не робить.
        """
        if not diff or not diff.get('has_changes'):
            return False
        if not self.is_configured():
            return False
        message = self.format_order_edit_message(order, diff, changed_by=changed_by)
        return bool(self.send_message(message))

    def send_admin_status_update(self, order, old_status, new_status):
        """Отправляет админу уведомление об изменении статусу замовлення"""
        if not self.is_configured():
            return False

        message = f"📊 <b>Оновлення замовлення #{order.order_number}</b>\n\n"
        message += f"👤 {order.full_name}\n"
        message += f"📞 {order.phone}\n\n"
        message += f"Статус змінено: <b>{old_status}</b> → <b>{new_status}</b>\n"
        message += f"⏰ {timezone.now().strftime('%d.%m.%Y %H:%M')}"

        return self.send_message(message)

    def send_admin_payment_status_update(self, order, old_status, new_status, pay_type=None):
        """Отправляет админу уведомление об изменении статусу оплати"""
        if not self.is_configured():
            return False

        return self.send_message(self.format_admin_payment_status_update(
            order,
            old_status=old_status,
            new_status=new_status,
            pay_type=pay_type,
        ))

    def format_admin_payment_status_update(self, order, old_status, new_status, pay_type=None):
        """Build a clear payment event message with gross, discount and paid values."""

        status_map = {
            'paid': 'Оплачено повністю',
            'prepaid': 'Внесена передплата',
            'partial': 'Внесена передплата',
            'checking': 'На перевірці',
            'unpaid': 'Не оплачено',
        }
        pay_type_map = {
            'online_full': 'Онлайн оплата (повна сума)',
            'full': 'Онлайн оплата (повна сума)',
            'prepay_200': 'Передплата 200 грн',
            'partial': 'Передплата 200 грн',
            'cod': 'Оплата при отриманні',
        }

        pay_type_label = pay_type_map.get(pay_type or getattr(order, 'pay_type', ''), pay_type or getattr(order, 'pay_type', '—'))
        old_display = status_map.get(old_status, old_status or '—')
        new_display = status_map.get(new_status, new_status or '—')

        snapshot = build_order_payment_snapshot(order)
        gross = self._fmt_amount(snapshot['gross_total_value'])
        discount = self._fmt_amount(snapshot['discount_amount_value'])
        payable = self._fmt_amount(snapshot['payable_total_value'])
        prepayment = self._fmt_amount(snapshot['prepayment_amount_value'])
        remaining = self._fmt_amount(snapshot['remaining_amount_value'])
        successful = new_status in ('paid', 'prepaid', 'partial')
        heading = 'ОПЛАТУ ОТРИМАНО' if successful else 'ОПЛАТА ОНОВЛЕНА'
        paid_now = prepayment if new_status in ('prepaid', 'partial') else payable

        message = f"""💳 <b>{heading} #{order.order_number}</b>

👤 {order.full_name}
📞 {order.phone}
📍 {order.city}, {order.np_office}

Тип оплати: {pay_type_label}
Статус: {old_display} → <b>{new_display}</b>
Сума товарів: {gross} грн"""

        if snapshot['discount_amount_value'] > 0:
            promo = f" ({order.promo_code.code})" if getattr(order, 'promo_code_id', None) else ''
            message += f"\nЗнижка: -{discount} грн{promo}"
        message += f"\nДо сплати: {payable} грн"
        if successful:
            message += f"\n✅ Сплачено: {paid_now} грн"
        if new_status in ('prepaid', 'partial'):
            message += f"\n📦 Залишок при отриманні: {remaining} грн"

        return message

    def send_admin_ttn_added_notification(self, order):
        """
        Отправляет админу уведомление о добавлении ТТН к заказу

        Args:
            order (Order): Заказ с добавленным ТТН

        Returns:
            bool: True если сообщение отправлено успешно
        """
        if not self.is_configured():
            return False

        if not order.user:
            return False

        # Получаем Telegram username из профиля пользователя
        try:
            from accounts.models import UserProfile
            profile = UserProfile.objects.get(user=order.user)
            telegram_username = profile.telegram

            if not telegram_username:
                return False

            # Формируем сообщение
            message = self._format_ttn_added_message(order)

            # Отправляем сообщение с упоминанием пользователя
            if telegram_username.startswith('@'):
                telegram_username = telegram_username[1:]

            full_message = f"@{telegram_username}\n\n{message}"
            return self.send_message(full_message)

        except Exception as e:
            print(f"Ошибка при отправке уведомления о ТТН: {e}")
            return False

    def _format_ttn_added_message(self, order):
        """
        Форматирует сообщение о добавлении ТТН

        Args:
            order (Order): Заказ с ТТН

        Returns:
            str: Отформатированное сообщение
        """
        message = f"""📦 <b>ТТН ДОДАНО ДО ЗАМОВЛЕННЯ</b>

🆔 <b>Замовлення:</b> #{order.order_number}
📋 <b>ТТН:</b> {order.tracking_number}

📊 <b>Статус замовлення:</b> {order.get_status_display()}
💰 <b>Сума:</b> {order.total_sum} грн

🕐 <b>Час додавання:</b> {timezone.now().strftime('%d.%m.%Y %H:%M')}

<i>Тепер ви можете відстежувати статус вашої посилки!</i>

🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Допомога в Telegram</a>
• <a href="https://twocomms.shop/my-orders/">📋 Мої замовлення</a>"""

        return message

    def send_ttn_added_notification(self, order):
        """Отправляет уведомление пользователю о добавлении ТТН"""
        if not order.user or not order.user.userprofile.telegram_id:
            return False

        message = self._format_ttn_added_message(order)
        return self.send_personal_message(order.user.userprofile.telegram_id, message)

    def send_order_status_update(self, order, old_status, new_status):
        """Отправляет уведомление пользователю об изменении статуса заказа"""
        if not order.user or not order.user.userprofile.telegram_id:
            return False

        message = self._format_status_update_message(order, old_status, new_status)
        return self.send_personal_message(order.user.userprofile.telegram_id, message)

    def send_ttn_notification(self, order):
        """Отправляет уведомление дропшиперу о добавлении ТТН"""
        if not order.dropshipper or not order.dropshipper.userprofile.telegram_id:
            return False

        if not order.tracking_number:
            return False

        message = f"""📦 <b>ТТН ДОДАНО ДО ВАШОГО ЗАМОВЛЕННЯ!</b>

🆔 <b>Замовлення:</b> #{order.order_number}

🚚 <b>Номер ТТН:</b> <code>{order.tracking_number}</code>

📍 <b>Клієнт:</b> {order.client_name}
📞 <b>Телефон:</b> {order.client_phone}
🏢 <b>Адреса:</b> {order.client_np_address or '—'}

💰 <b>Ваш прибуток:</b> {order.profit} грн

🔗 <b>Відстежити посилку:</b>
<a href="https://novaposhta.ua/tracking/?cargo_number={order.tracking_number}">Відкрити на сайті Нової Пошти</a>

<i>Тепер ви можете відстежувати статус доставки!</i>"""

        return self.send_personal_message(order.dropshipper.userprofile.telegram_id, message)

    def _format_status_update_message(self, order, old_status, new_status):
        """Форматирует сообщение об изменении статуса заказа"""
        message = f"""📋 <b>ОНОВЛЕННЯ СТАТУСУ ЗАМОВЛЕННЯ</b>

🆔 <b>Замовлення:</b> #{order.order_number}

📊 <b>Статус змінено:</b>
├─ Було: {old_status}
└─ Стало: <b>{new_status}</b>

💰 <b>Сума:</b> {order.total_sum} грн

🕐 <b>Час оновлення:</b> {timezone.now().strftime('%d.%m.%Y %H:%M')}

<i>Слідкуйте за оновленнями вашого замовлення!</i>

🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Допомога в Telegram</a>
• <a href="https://twocomms.shop/my-orders/">📋 Мої замовлення</a>"""

        return message

    def send_invoice_notification(self, invoice):
        """
        Отправляет уведомление о новой накладной админу

        Args:
            invoice (WholesaleInvoice): Накладная

        Returns:
            bool: True если сообщение отправлено успешно
        """
        if not self.is_configured():
            return False

        message = self.format_invoice_message(invoice)
        return self.send_message(message)

    def format_invoice_message(self, invoice):
        """
        Форматирует сообщение о накладной

        Args:
            invoice (WholesaleInvoice): Накладная

        Returns:
            str: Отформатированное сообщение
        """
        # Основная информация о накладной
        invoice_header = f"📋 <b>НОВА НАКЛАДНА #{invoice.invoice_number}</b>\n"

        # Создаем блок с информацией о накладной
        full_block = f"""
<pre language="text">
┌─────────────────────────────────────────┐
│  📋 НАКЛАДНА #{invoice.invoice_number}                │
├─────────────────────────────────────────┤
│  🏢 КОМПАНІЯ:
│     Назва: {invoice.company_name}
│     Номер: {invoice.company_number or 'Не вказано'}
│     Телефон: {invoice.contact_phone}
│     Адреса: {invoice.delivery_address}
├─────────────────────────────────────────┤
│  📊 ДЕТАЛІ НАКЛАДНОЇ:
│     Статус: {invoice.get_status_display()}
│     Футболки: {invoice.total_tshirts} шт.
│     Худі: {invoice.total_hoodies} шт.
│     Загальна сума: {invoice.total_amount} грн
│     Дата створення: {invoice.created_at.strftime('%d.%m.%Y %H:%M')}
└─────────────────────────────────────────┘
</pre>"""

        # Добавляем ссылки
        links = f"""
🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Допомога в Telegram</a>
• <a href="https://twocomms.shop/admin-panel/collaboration/">📋 Адмін панель</a>
• <a href="https://twocomms.shop/wholesale/download-invoice/{invoice.id}/">📥 Скачати накладну</a>"""

        # Собираем полное сообщение
        message = f"{invoice_header}\n{full_block}\n{links}"

        return message

    def send_invoice_document(self, invoice, file_path):
        """
        Отправляет документ накладной в Telegram

        Args:
            invoice (WholesaleInvoice): Накладная
            file_path (str): Путь к файлу накладной

        Returns:
            bool: True если документ отправлен успешно
        """
        if not self.is_configured():
            return False

        # Используем админ ID, если он доступен, иначе chat_id
        target_ids = self.admin_ids or self.chat_ids
        if not target_ids:
            return False

        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"

            # Читаем файл
            success = False
            for target_id in target_ids:
                with open(file_path, 'rb') as file:
                    files = {'document': file}
                    data = {
                        'chat_id': target_id,
                        'caption': f"📋 Накладна #{invoice.invoice_number}\n🏢 {invoice.company_name}\n💰 {invoice.total_amount} грн",
                        'parse_mode': 'HTML'
                    }
                    response = requests.post(url, files=files, data=data, timeout=30)
                    success = success or response.status_code == 200
            return success
        except Exception as e:
            print(f"Ошибка при отправке документа накладной: {e}")
            return False

    def send_dropshipper_order_notification(self, order):
        """
        Отправляет уведомление админу о новом заказе дропшиппера

        Args:
            order (DropshipperOrder): Заказ дропшиппера

        Returns:
            bool: True если сообщение отправлено успешно
        """
        if not self.is_configured():
            return False

        message = self._format_dropshipper_order_message(order)
        return self.send_message(message)

    def _format_dropshipper_order_message(self, order):
        """
        Форматирует сообщение о новом заказе дропшиппера

        Args:
            order (DropshipperOrder): Заказ дропшиппера

        Returns:
            str: Отформатированное HTML сообщение
        """
        # Определяем эмодзи статуса
        status_emoji = "📋"
        if order.status == 'draft':
            status_emoji = "⏳"
        elif order.status == 'pending':
            status_emoji = "🔔"
        elif order.status == 'confirmed':
            status_emoji = "✅"

        # Заголовок
        header = f"🆕 <b>НОВИЙ ДРОПШИП ЗАМОВЛЕННЯ</b>\n"

        # Получаем информацию о дропшиппере
        dropshipper_profile = None
        try:
            dropshipper_profile = order.dropshipper.userprofile
        except Exception:
            pass

        dropshipper_company = dropshipper_profile.company_name if dropshipper_profile and dropshipper_profile.company_name else order.dropshipper.username
        dropshipper_telegram = dropshipper_profile.telegram if dropshipper_profile and dropshipper_profile.telegram else 'не підключено'
        dropshipper_phone = dropshipper_profile.phone if dropshipper_profile and dropshipper_profile.phone else 'не вказано'

        # Основной блок информации
        full_block = f"""
<pre language="text">
┌─────────────────────────────────────────┐
│  {status_emoji} ЗАМОВЛЕННЯ #{order.order_number}
├─────────────────────────────────────────┤
│  👤 ДРОПШИПЕР:
│     Компанія: {dropshipper_company}
│     Telegram: @{dropshipper_telegram}
│     Телефон: {dropshipper_phone}
├─────────────────────────────────────────┤
│  📦 КЛІЄНТ:
│     ПІБ: {order.client_name if order.client_name else 'Не вказано'}
│     Телефон: {order.client_phone if order.client_phone else 'Не вказано'}"""

        if order.client_np_address:
            full_block += f"\n│     Адреса НП: {order.client_np_address}"

        full_block += f"""
├─────────────────────────────────────────┤
│  📋 СТАТУС:
│     Замовлення: {order.get_status_display()}
│     Оплата: {order.get_payment_status_display()}
│     Створено: {order.created_at.strftime('%d.%m.%Y %H:%M')}
├─────────────────────────────────────────┤
│  📦 ТОВАРИ ({order.items.count()} позицій):
"""

        # Добавляем товары
        for i, item in enumerate(order.items.all(), 1):
            full_block += f"│     {i}. {item.product.title}\n"

            # Детали товара
            if item.size:
                full_block += f"│        └ Розмір: {item.size}\n"
            if item.fit_label:
                full_block += f"│        └ Посадка: {item.fit_label}\n"
            for option_label in getattr(item, "generic_option_labels", []) or []:
                full_block += f"│        └ {option_label}\n"
            if item.quantity:
                full_block += f"│        └ Кількість: {item.quantity}\n"
            if item.color_variant:
                full_block += f"│        └ Колір: {item.color_variant.color.name if hasattr(item.color_variant.color, 'name') else str(item.color_variant.color)}\n"
            if item.drop_price:
                full_block += f"│        └ Ціна дропа: {item.drop_price} грн\n"
            if item.selling_price:
                full_block += f"│        └ Ціна продажу: {item.selling_price} грн\n"

            if i < order.items.count():
                full_block += "│     ───────────────────────────────────\n"

        # Информация об оплате
        payment_method_text = "💳 Оплачено передоплатою" if order.payment_method == 'prepaid' else "📦 Накладний платіж"
        dropshipper_payment = order.dropshipper_payment_required if order.dropshipper_payment_required else 0

        # Итоговая информация
        full_block += f"""├─────────────────────────────────────────┤
│  💰 ФІНАНСИ:
│     Сума продажу: {order.total_selling_price} грн
│     Собівартість: {order.total_drop_price} грн
│     Прибуток дропшипера: {order.profit} грн
├─────────────────────────────────────────┤
│  💳 ОПЛАТА:
│     Спосіб: {payment_method_text}
│     Дропшипер сплачує: {dropshipper_payment} грн"""

        if order.payment_method == 'cod':
            full_block += "\n│     📦 Віднімається з накладки при отриманні"

        full_block += f"""
├─────────────────────────────────────────┤
│  💳 ВСЬОГО: {order.total_selling_price} грн
└─────────────────────────────────────────┘
</pre>"""

        # Добавляем ссылки
        links = f"""
🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Підтримка в Telegram</a>
• <a href="https://twocomms.shop/admin/orders/dropshipperorder/{order.id}/change/">⚙️ Керування замовленням</a>
• <a href="https://twocomms.shop/orders/dropshipper/orders/">📋 Всі замовлення дропшипера</a>
"""

        # Дополнительная информация
        additional_info = ""
        if order.status == 'draft':
            additional_info = "\n⚠️ <b>Увага!</b> Замовлення потребує підтвердження дропшипером."

        # Добавляем источник и примечания если есть
        if order.order_source:
            additional_info += f"\n🔗 <b>Джерело замовлення:</b> {order.order_source}"

        if order.notes:
            additional_info += f"\n📝 <b>Примітки:</b> {order.notes}"

        # Собираем полное сообщение
        message = f"{header}\n{full_block}\n{links}{additional_info}"

        return message

    def send_dropshipper_payment_notification(self, order):
        """
        Отправляет уведомление админу И дропшиперу об оплате дропшип заказа

        Args:
            order (DropshipperOrder): Оплаченный заказ дропшиппера

        Returns:
            bool: True если хотя бы одно сообщение отправлено успешно
        """
        print(f"🔵 send_dropshipper_payment_notification called for order #{order.order_number}")

        if not self.is_configured():
            print(f"❌ Telegram notifier not configured")
            return False

        # Отправляем админу
        admin_message = self._format_dropshipper_payment_message(order)
        print(f"🟡 Sending payment notification to ADMIN")
        admin_result = self.send_message(admin_message)
        print(f"{'✅' if admin_result else '❌'} Admin notification result: {admin_result}")

        # Отправляем дропшиперу
        dropshipper_result = False
        if order.dropshipper:
            try:
                telegram_id = order.dropshipper.userprofile.telegram_id
                print(f"🟡 Dropshipper telegram_id: {telegram_id}")

                if telegram_id:
                    # Формируем сообщение для дропшипера
                    dropshipper_message = f"""💰 <b>ВАШ ЗАКАЗ ОПЛАЧЕНО!</b>

Замовлення <b>#{order.order_number}</b> успішно оплачено клієнтом!

💳 <b>Сума оплати:</b> {order.dropshipper_payment_required} грн
📦 <b>Товарів:</b> {order.items.count()} позицій
💵 <b>Ваш прибуток:</b> {order.profit} грн

Замовлення очікує на підтвердження адміністратором для відправки.

🔗 <a href="https://twocomms.shop/orders/dropshipper/?tab=orders">Переглянути замовлення</a>"""

                    print(f"🟢 Sending payment notification to DROPSHIPPER (telegram_id={telegram_id})")
                    dropshipper_result = self.send_personal_message(telegram_id, dropshipper_message)
                    print(f"{'✅' if dropshipper_result else '❌'} Dropshipper notification result: {dropshipper_result}")
                else:
                    print(f"⚠️ Dropshipper {order.dropshipper.username} has no telegram_id")
            except Exception as e:
                print(f"❌ Error sending to dropshipper: {e}")

        # Возвращаем True если хотя бы одно уведомление отправлено
        return admin_result or dropshipper_result

    def _format_dropshipper_payment_message(self, order):
        """
        Форматирует сообщение об оплате дропшип заказа

        Args:
            order (DropshipperOrder): Оплаченный заказ дропшиппера

        Returns:
            str: Отформатированное HTML сообщение
        """
        # Получаем информацию о дропшиппере
        dropshipper_profile = None
        try:
            dropshipper_profile = order.dropshipper.userprofile
        except Exception:
            pass

        dropshipper_company = dropshipper_profile.company_name if dropshipper_profile and dropshipper_profile.company_name else order.dropshipper.username

        # Заголовок с эмодзи
        header = f"💰 <b>ДРОПШИП ЗАМОВЛЕННЯ ОПЛАЧЕНО!</b>\n"

        # Основной блок информации
        full_block = f"""
<pre language="text">
┌─────────────────────────────────────────┐
│  ✅ ЗАМОВЛЕННЯ #{order.order_number}
├─────────────────────────────────────────┤
│  👤 ДРОПШИПЕР:
│     Компанія: {dropshipper_company}
├─────────────────────────────────────────┤
│  📦 КЛІЄНТ:
│     ПІБ: {order.client_name if order.client_name else 'Не вказано'}
│     Телефон: {order.client_phone if order.client_phone else 'Не вказано'}
├─────────────────────────────────────────┤
│  💳 ОПЛАТА:
│     Статус: ОПЛАЧЕНО ✅
│     Сума: {order.dropshipper_payment_required} грн
│     Спосіб: {order.get_payment_method_display()}
│     Час оплати: {timezone.now().strftime('%d.%m.%Y %H:%M')}
├─────────────────────────────────────────┤
│  📦 ТОВАРИ ({order.items.count()} позицій):
"""

        # Добавляем товары
        for i, item in enumerate(order.items.all(), 1):
            full_block += f"│     {i}. {item.product.title}"
            if item.size:
                full_block += f" · {item.size}"
            if item.color_variant:
                full_block += f" · {item.color_variant.color.name if hasattr(item.color_variant.color, 'name') else str(item.color_variant.color)}"
            full_block += f" (×{item.quantity})\n"

        full_block += f"""├─────────────────────────────────────────┤
│  💰 ФІНАНСИ:
│     Сума продажу: {order.total_selling_price} грн
│     Собівартість: {order.total_drop_price} грн
│     Прибуток дропшипера: {order.profit} грн
└─────────────────────────────────────────┘
</pre>"""

        # Добавляем ссылки
        links = f"""
🔗 <b>Корисні посилання:</b>
• <a href="https://t.me/twocomms">💬 Підтримка в Telegram</a>
• <a href="https://twocomms.shop/admin/orders/dropshipperorder/{order.id}/change/">⚙️ Керування замовленням</a>

✅ <i>Замовлення готове до відправки!</i>
"""

        # Собираем полное сообщение
        message = f"{header}\n{full_block}\n{links}"

        return message

    def send_dropshipper_order_created_notification(self, order):
        """
        Отправляет уведомление дропшиперу о создании его заказа

        Args:
            order (DropshipperOrder): Созданный заказ дропшиппера

        Returns:
            bool: True если сообщение отправлено успешно
        """
        print(f"🔵 send_dropshipper_order_created_notification called for order #{order.order_number}")
        print(f"🔵 Dropshipper: {order.dropshipper.username if order.dropshipper else 'None'}")

        if not order.dropshipper:
            print(f"❌ No dropshipper for order #{order.order_number}")
            return False

        try:
            telegram_id = order.dropshipper.userprofile.telegram_id
            print(f"🟡 Dropshipper telegram_id: {telegram_id}")
        except Exception as e:
            print(f"❌ Error getting telegram_id: {e}")
            return False

        if not telegram_id:
            print(f"❌ Dropshipper {order.dropshipper.username} has no telegram_id")
            return False

        message = self._format_dropshipper_order_created_message(order)
        print(f"🟢 Sending message to telegram_id={telegram_id}")
        result = self.send_personal_message(telegram_id, message)
        print(f"{'✅' if result else '❌'} Message send result: {result}")
        return result

    def _format_dropshipper_order_created_message(self, order):
        """
        Форматирует сообщение о создании заказа для дропшипера

        Args:
            order (DropshipperOrder): Созданный заказ дропшиппера

        Returns:
            str: Отформатированное HTML сообщение
        """
        # Определяем эмодзи статуса
        status_emoji = "⏳" if order.status == 'pending' else "✅"

        # Заголовок
        header = f"🆕 <b>НОВЕ ЗАМОВЛЕННЯ СТВОРЕНО!</b>\n"

        # Основной блок информации
        full_block = f"""
<pre language="text">
┌─────────────────────────────────────────┐
│  {status_emoji} ЗАМОВЛЕННЯ #{order.order_number}
├─────────────────────────────────────────┤
│  📦 КЛІЄНТ:
│     ПІБ: {order.client_name if order.client_name else 'Не вказано'}
│     Телефон: {order.client_phone if order.client_phone else 'Не вказано'}"""

        if order.client_np_address:
            full_block += f"\n│     Адреса НП: {order.client_np_address}"

        full_block += f"""
├─────────────────────────────────────────┤
│  📋 СТАТУС:
│     Замовлення: {order.get_status_display()}"""

        if order.status == 'pending':
            full_block += "\n│     ⏳ Очікує підтвердження адміністратором"
        elif order.status == 'awaiting_payment':
            full_block += "\n│     💳 Готове до оплати"
        elif order.status == 'confirmed':
            full_block += "\n│     ✅ Підтверджено, готується до відправки"

        full_block += f"""
│     Оплата: {order.get_payment_status_display()}
│     Створено: {order.created_at.strftime('%d.%m.%Y %H:%M')}
├─────────────────────────────────────────┤
│  📦 ТОВАРИ ({order.items.count()} позицій):
"""

        # Добавляем товары
        for i, item in enumerate(order.items.all(), 1):
            full_block += f"│     {i}. {item.product.title}"
            if item.size:
                full_block += f" · {item.size}"
            if item.color_variant:
                full_block += f" · {item.color_variant.color.name if hasattr(item.color_variant.color, 'name') else str(item.color_variant.color)}"
            full_block += f" (×{item.quantity})\n"

        full_block += f"""├─────────────────────────────────────────┤
│  💰 ФІНАНСИ:
│     Сума продажу: {order.total_selling_price} грн
│     Собівартість: {order.total_drop_price} грн
│     ВАШ ПРИБУТОК: {order.profit} грн 🎉
├─────────────────────────────────────────┤
│  💳 ОПЛАТА:
│     Спосіб: {order.get_payment_method_display()}"""

        if order.payment_method == 'prepaid' and order.payment_status == 'unpaid':
            full_block += f"\n│     До сплати: {order.dropshipper_payment_required} грн"
        elif order.payment_method == 'cod':
            full_block += "\n│     📦 200 грн віднімається з накладки"
        elif order.payment_method == 'delegation':
            full_block += "\n│     🎁 Ризики на нас, ви не платите"

        full_block += f"""
└─────────────────────────────────────────┘
</pre>"""

        # Добавляем информацию о следующих шагах
        next_steps = ""
        if order.status == 'pending':
            next_steps = """
<b>📝 Наступні кроки:</b>
1️⃣ Дочекайтеся підтвердження адміністратора
2️⃣ Після підтвердження ви зможете оплатити замовлення
3️⃣ Отримаєте сповіщення про зміну статусу

⏳ <i>Зазвичай підтвердження займає до 1 години</i>"""
        elif order.status == 'awaiting_payment' and order.payment_status == 'unpaid':
            next_steps = f"""
<b>📝 Наступні кроки:</b>
1️⃣ Оплатіть замовлення ({order.dropshipper_payment_required} грн)
2️⃣ Після оплати замовлення буде підтверджено
3️⃣ Отримаєте ТТН для відстеження

💳 <i>Перейдіть до розділу замовлень для оплати</i>"""

        # Добавляем ссылки
        links = f"""
🔗 <b>Корисні посилання:</b>
• <a href="https://twocomms.shop/orders/dropshipper/orders/">📋 Переглянути замовлення</a>
• <a href="https://twocomms.shop/orders/dropshipper/dashboard/">🏠 Дропшип панель</a>
"""

        # Собираем полное сообщение
        message = f"{header}\n{full_block}\n{next_steps}\n{links}"

        return message

    def send_dropshipper_status_change_notification(self, order, old_status, new_status):
        """
        Отправляет уведомление дропшиперу об изменении статуса заказа

        Args:
            order (DropshipperOrder): Заказ дропшиппера
            old_status: Старый статус
            new_status: Новый статус

        Returns:
            bool: True если сообщение отправлено успешно
        """
        print(f"🔵 send_dropshipper_status_change_notification called for order #{order.order_number}")
        print(f"🔵 Status change: {old_status} → {new_status}")
        print(f"🔵 Dropshipper: {order.dropshipper.username if order.dropshipper else 'None'}")

        if not order.dropshipper:
            print(f"❌ No dropshipper for order #{order.order_number}")
            return False

        try:
            telegram_id = order.dropshipper.userprofile.telegram_id
            print(f"🟡 Dropshipper telegram_id: {telegram_id}")
        except Exception as e:
            print(f"❌ Error getting telegram_id: {e}")
            return False

        if not telegram_id:
            print(f"❌ Dropshipper {order.dropshipper.username} has no telegram_id")
            return False

        message = self._format_dropshipper_status_change_message(order, old_status, new_status)
        print(f"🟢 Sending status change notification to telegram_id={telegram_id}")
        result = self.send_personal_message(telegram_id, message)
        print(f"{'✅' if result else '❌'} Status change notification result: {result}")
        return result

    def _format_dropshipper_status_change_message(self, order, old_status, new_status):
        """
        Форматирует сообщение об изменении статуса для дропшипера

        Args:
            order (DropshipperOrder): Заказ дропшиппера
            old_status: Старый статус
            new_status: Новый статус

        Returns:
            str: Отформатированное HTML сообщение
        """
        # Определяем эмодзи для нового статуса
        status_emoji_map = {
            'pending': '⏳',
            'awaiting_payment': '💳',
            'confirmed': '✅',
            'awaiting_shipment': '📦',
            'shipped': '🚚',
            'delivered_awaiting_pickup': '📬',
            'received': '🎉',
            'refused': '❌',
            'cancelled': '🚫'
        }

        new_status_emoji = status_emoji_map.get(new_status, '📋')

        # Заголовок
        header = f"{new_status_emoji} <b>ЗМІНА СТАТУСУ ЗАМОВЛЕННЯ!</b>\n"

        # Основной блок информации
        full_block = f"""
<pre language="text">
┌─────────────────────────────────────────┐
│  📋 ЗАМОВЛЕННЯ #{order.order_number}
├─────────────────────────────────────────┤
│  📊 СТАТУС ЗМІНЕНО:
│     Було: {order.STATUS_CHOICES_DICT.get(old_status, old_status)}
│     Стало: {order.get_status_display()} {new_status_emoji}
├─────────────────────────────────────────┤
│  📦 КЛІЄНТ:
│     ПІБ: {order.client_name if order.client_name else 'Не вказано'}
│     Телефон: {order.client_phone if order.client_phone else 'Не вказано'}"""

        if order.tracking_number:
            full_block += f"""
├─────────────────────────────────────────┤
│  🚚 ТТН:
│     Номер: {order.tracking_number}"""

        full_block += f"""
├─────────────────────────────────────────┤
│  💰 ФІНАНСИ:
│     ВАШ ПРИБУТОК: {order.profit} грн
│     Оплата: {order.get_payment_status_display()}
└─────────────────────────────────────────┘
</pre>"""

        # Добавляем информацию о следующих шагах в зависимости от нового статуса
        next_steps = ""
        if new_status == 'awaiting_payment':
            next_steps = f"""
<b>📝 Наступні кроки:</b>
1️⃣ Оплатіть замовлення ({order.dropshipper_payment_required} грн)
2️⃣ Після оплати замовлення буде підтверджено
3️⃣ Отримаєте ТТН для відстеження

💳 <i>Замовлення готове до оплати!</i>"""
        elif new_status == 'confirmed':
            next_steps = """
<b>📝 Наступні кроки:</b>
1️⃣ Замовлення готується до відправки
2️⃣ Очікуйте ТТН для відстеження
3️⃣ Отримаєте сповіщення про відправку

✅ <i>Замовлення підтверджено!</i>"""
        elif new_status == 'shipped':
            next_steps = f"""
<b>📝 Наступні кроки:</b>
1️⃣ Відстежуйте посилку за ТТН: {order.tracking_number if order.tracking_number else 'очікується'}
2️⃣ Повідомте клієнта про відправку
3️⃣ Отримаєте сповіщення про доставку

🚚 <i>Замовлення відправлено!</i>"""
        elif new_status == 'delivered_awaiting_pickup':
            next_steps = """
<b>📝 Наступні кроки:</b>
1️⃣ Повідомте клієнта про прибуття посилки
2️⃣ Очікуйте отримання клієнтом
3️⃣ Прибуток буде зараховано після отримання

📬 <i>Посилка прибула!</i>"""
        elif new_status == 'received':
            next_steps = """
<b>📝 Ваші дії:</b>
✅ Замовлення успішно виконано!
💰 Ваш прибуток зараховано на баланс
📊 Перевірте розділ "Виплати"

🎉 <i>Вітаємо з успішною угодою!</i>"""
        elif new_status == 'refused':
            next_steps = """
<b>📝 Що сталося:</b>
❌ Клієнт відмовився від товару
📦 Товар повертається на склад
💰 Виплату скасовано

😔 <i>Не переживайте, ви зможете продати товар знову!</i>"""

        # Добавляем ссылки
        links = f"""
🔗 <b>Корисні посилання:</b>
• <a href="https://twocomms.shop/orders/dropshipper/orders/">📋 Переглянути замовлення</a>
• <a href="https://twocomms.shop/orders/dropshipper/statistics/">📊 Статистика</a>
"""

        if order.tracking_number:
            links += f"• <a href=\"https://novaposhta.ua/tracking/?cargo_number={order.tracking_number}\">🚚 Відстежити посилку</a>\n"

        # Собираем полное сообщение
        message = f"{header}\n{full_block}\n{next_steps}\n{links}"

        return message


# Глобальный экземпляр для использования
telegram_notifier = TelegramNotifier()
