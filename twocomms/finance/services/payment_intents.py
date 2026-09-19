"""Payment Intent state machine.

Monobank Personal API currently exposes statements and webhooks in this
project.  The fallback therefore produces verified bank instructions and
waits for a matching statement item instead of claiming that money was sent.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from ..models import PaymentIntent, PaymentIntentEvent, Transaction

ALLOWED = {
    'draft': {'awaiting_confirmation', 'rejected', 'expired'},
    'awaiting_confirmation': {'submitted', 'rejected', 'expired'},
    'submitted': {'detected', 'rejected', 'expired'},
    'detected': {'confirmed', 'rejected'},
    'confirmed': set(), 'rejected': set(), 'expired': set(),
}


def capability(provider='monobank_personal'):
    return {'provider': provider, 'outgoing_supported': False,
            'mode': 'prefilled_instruction',
            'message': 'Официальный исходящий API не подключён; ожидается подтверждение по выписке.'}


@db_transaction.atomic
def create_intent(*, company, account, amount, component=None, recipient_card=None,
                  purpose='', comment='', user=None, idempotency_key=None):
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError('Сумма должна быть больше 0')
    key = idempotency_key or uuid.uuid4().hex
    intent, created = PaymentIntent.objects.get_or_create(
        idempotency_key=key,
        defaults={
            'company': company, 'account': account, 'amount': amount,
            'currency': account.currency, 'component': component,
            'recipient_card': recipient_card, 'purpose': purpose or '',
            'comment': comment or '',
            'created_by': user if getattr(user, 'is_authenticated', False) else None,
        },
    )
    return intent, created


@db_transaction.atomic
def transition(intent, status, *, user=None, payload=None):
    if status not in ALLOWED.get(intent.status, set()):
        raise ValueError(f'Переход {intent.status} → {status} запрещён')
    old = intent.status
    intent.status = status
    intent.save(update_fields=['status', 'updated_at'])
    PaymentIntentEvent.objects.create(
        intent=intent, from_status=old, to_status=status,
        payload=payload or {}, created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    return intent


def instruction_payload(intent):
    card = intent.recipient_card
    return {
        'id': intent.id, 'status': intent.status, 'capability': capability(intent.provider),
        'amount': str(intent.amount), 'currency': intent.currency,
        'recipient': {'label': card.label if card else '', 'bank': card.bank if card else '',
                      'iban': card.iban if card else '', 'pan_mask': card.pan_mask if card else ''},
        'purpose': intent.purpose, 'comment': intent.comment,
    }


def match_statement(intent, txn):
    if txn.status != Transaction.STATUS_ACTUAL or txn.type != Transaction.TYPE_EXPENSE:
        return False
    if txn.account_id != intent.account_id or txn.amount != intent.amount:
        return False
    if intent.recipient_card and intent.recipient_card.iban:
        counter_iban = (txn.external_data or {}).get('counterIban') or (txn.external_data or {}).get('counter_iban')
        if counter_iban and counter_iban != intent.recipient_card.iban:
            return False
    return True


@db_transaction.atomic
def attach_statement(intent, txn, *, user=None):
    if intent.status not in {'submitted', 'awaiting_confirmation'} or not match_statement(intent, txn):
        raise ValueError('Операция не соответствует платёжному намерению')
    intent.matched_transaction = txn
    intent.save(update_fields=['matched_transaction', 'updated_at'])
    transition(intent, 'detected', user=user, payload={'transaction_id': txn.id})
    return intent


def match_pending_intents(txn, *, user=None):
    """Attach a newly imported bank expense to matching pending intents."""
    matches = []
    qs = PaymentIntent.objects.filter(
        company=txn.company, account=txn.account,
        status__in=['submitted', 'awaiting_confirmation'],
    ).order_by('created_at')
    for intent in qs:
        if not match_statement(intent, txn):
            continue
        try:
            matches.append(attach_statement(intent, txn, user=user))
        except ValueError:
            continue
        # One statement item can confirm at most one idempotent intent.
        break
    return matches
