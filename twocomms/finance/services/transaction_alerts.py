"""Immediate, non-mutating alerts for imported transactions that need review."""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db import transaction as db_transaction
from django.db.models import Q
from urllib.parse import urlencode

from ..models import Transaction
from ..models_settings import UserSettings


_CACHE_PREFIX = 'finance:incoming-classification-alert:v1'


def _finance_base_url() -> str:
    base = getattr(settings, 'FINANCE_PUBLIC_BASE', '') or getattr(settings, 'FIN_PUBLIC_BASE', '')
    if base:
        return base.rstrip('/')
    host = getattr(settings, 'FIN_HOST', '') or 'fin.twocomms.shop'
    return f'https://{host}'


def _is_unclassified_incoming(txn) -> bool:
    return (
        txn.status == Transaction.STATUS_ACTUAL
        and txn.type == Transaction.TYPE_INCOME
        and txn.economic_kind == 'unknown'
    )


def _alert_content(txn) -> tuple[str, str, str]:
    from .ledger_v2 import terminal_cash_evidence

    amount = f'{txn.amount:.2f} {txn.currency}'
    terminal = terminal_cash_evidence(txn)['is_candidate']
    if terminal:
        return (
            'Потрібна класифікація поповнення',
            f'Поповнення через термінал Mono/City24 на {amount} поки враховане як дохід і може впливати на P&L. Підтвердьте класифікацію.',
            f'/payments/?terminal_review={txn.id}',
        )
    return (
        'Потрібна класифікація надходження',
        f'Нове надходження на {amount} поки враховане як дохід і може впливати на P&L. Перевірте класифікацію.',
        f'/payments/?transaction={txn.id}',
    )


def _action_url(path: str, action: str) -> str:
    separator = '&' if '?' in path else '?'
    return f'{path}{separator}{urlencode({"classification_action": action})}'


def _claim(channel: str, txn_id: int, recipient: str) -> bool:
    """Atomically reserve one delivery attempt without changing the transaction."""
    key = f'{_CACHE_PREFIX}:{channel}:{txn_id}:{recipient}'
    try:
        return bool(cache.add(key, 1, timeout=None))
    except Exception:  # Cache failures must never turn an import into a send storm.
        return False


def _eligible_settings():
    return UserSettings.objects.filter(
        user__is_active=True,
    ).filter(Q(user__is_superuser=True) | Q(user__is_staff=True))


def notify_new_incoming_classification(txn_id: int) -> None:
    """Best-effort delivery for a committed imported incoming transaction.

    The cache claims are made before delivery so duplicate webhook/backfill calls
    cannot emit duplicate messages. Delivery failures are intentionally swallowed:
    the banking import remains authoritative and must complete independently.
    """
    txn = Transaction.objects.filter(pk=txn_id).select_related('account').first()
    if txn is None or not _is_unclassified_incoming(txn):
        return

    title, body, path = _alert_content(txn)
    settings_rows = list(_eligible_settings().select_related('user').order_by('user_id'))

    from . import push

    dedup_key = f'incoming-classification:{txn.id}'
    suggested_kind = 'sale' if txn.account and txn.account.is_business and not terminal else ''
    context = {
        'kind': 'classification_review',
        'transaction_id': txn.id,
        'suggested_kind': suggested_kind,
    }
    push_actions = [
        {'action': 'open', 'title': '📄 Відкрити операцію'},
        {'action': 'other', 'title': '↔ Обрати інше'},
    ]
    if suggested_kind:
        push_actions.insert(1, {'action': 'confirm', 'title': '✓ Так, це продаж'})
    for user_settings in settings_rows:
        if not user_settings.push_enabled or not _claim('push', txn.id, str(user_settings.user_id)):
            continue
        try:
            push.send_to_user(
                user_settings.user, title, body, url=path,
                tag=f'finance-classification-{txn.id}',
                notification_type='custom', dedup_key=dedup_key,
                report_data=context, actions=push_actions,
                require_interaction=True,
            )
        except Exception:
            continue

    if not any(row.telegram_notifications for row in settings_rows):
        return
    try:
        from management.services.notify import admin_chat_ids, send_message
        finance_url = f'{_finance_base_url()}{path}'
        buttons = [{'text': 'Відкрити у фінансах', 'url': finance_url}]
        if suggested_kind:
            buttons.extend([
                {'text': '✓ Так, це продаж', 'url': f'{_finance_base_url()}{_action_url(path, "confirm")}'},
                {'text': '↔ Обрати інше', 'url': f'{_finance_base_url()}{_action_url(path, "choose")}'},
            ])
        keyboard = {'inline_keyboard': [buttons]}
        for chat_id in admin_chat_ids():
            if not _claim('telegram', txn.id, str(chat_id)):
                continue
            try:
                send_message(chat_id, f'<b>{title}</b>\n\n{body}', reply_markup=keyboard)
            except Exception:
                continue
    except Exception:
        return


def schedule_new_incoming_classification_alert(txn) -> None:
    """Register delivery only after the import transaction has committed."""
    if not _is_unclassified_incoming(txn):
        return
    db_transaction.on_commit(lambda txn_id=txn.pk: notify_new_incoming_classification(txn_id))
