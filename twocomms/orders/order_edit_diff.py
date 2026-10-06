"""Побудова diff при редагуванні замовлення.

Чиста логіка (без сайд-ефектів), яку використовує view редагування
замовлення, щоб сформувати людяне Telegram-сповіщення «✏️ Замовлення
відредаговано» зі списком змін: що видалено, що додано, що змінилось
(кількість/ціна), а також зміни суми, доставки, оплати, клієнта.

Знімок (``snapshot_order``) робиться ДО видалення старих позицій, а потім
ПІСЛЯ збереження нових — і два знімки порівнюються ``build_order_edit_diff``.

Позиції зіставляються за «ключем ідентичності»:
* каталог — ``(product_id, color_variant_id, size)``;
* поза каталогом — ``(title, size, color_name)``.

Тому заміна кольору «Ментол → Чорний» коректно показується як одна
видалена і одна додана позиція (різні варіанти), а не як «зміна».
"""
from __future__ import annotations

from decimal import Decimal


def _item_key(item):
    """Стабільний ключ ідентичності позиції замовлення."""
    if getattr(item, 'is_dtf_film', False):
        return f'film|dtf_film|{Decimal(item.unit_price or 0):.2f}'
    if getattr(item, 'is_custom', False) or not getattr(item, 'product_id', None):
        color = (getattr(item, 'color_name_custom', '') or '').strip().lower()
        return f"cus|{(item.title or '').strip().lower()}|{(item.size or '').strip().lower()}|{color}"
    return f"cat|{item.product_id}|{item.color_variant_id or 0}|{(item.size or '').strip().lower()}"


def _item_label(item):
    """Людяний підпис позиції: «Назва · Колір · Розмір»."""
    parts = [(item.title or '').strip()]
    color = None
    try:
        color = item.color_name  # property: каталожний колір або кастомний
    except Exception:
        color = None
    if color:
        parts.append(str(color).strip())
    if (item.size or '').strip():
        parts.append((item.size or '').strip())
    label = ' · '.join(p for p in parts if p)
    if not getattr(item, 'is_dtf_film', False) and (getattr(item, 'is_custom', False) or not getattr(item, 'product_id', None)):
        label += ' (поза каталогом)'
    return label


def _delivery_text(order):
    if getattr(order, 'delivery_method', '') == 'handover':
        return f"Передача / самовивіз: {getattr(order, 'handover_details', '')}"
    return ', '.join(p for p in ((order.city or '').strip(), (order.np_office or '').strip()) if p)


def snapshot_order(order):
    """Серіалізує поточний стан замовлення для подальшого порівняння.

    Читає ``order.items.all()`` у пам'ять, тож після видалення позицій
    знімок лишається валідним.
    """
    items = []
    for item in order.items.all():
        items.append({
            'key': _item_key(item),
            'label': _item_label(item),
            'qty': int(item.qty or 0),
            'unit_price': Decimal(item.unit_price or 0),
            **({'item_kind': 'dtf_film', 'film_length_m': Decimal(item.film_length_m or 0)} if item.is_dtf_film else {}),
        })
    from orders.nova_poshta_documents import build_order_payment_snapshot

    payment = build_order_payment_snapshot(order)
    shipment_payment = {
        'payer_type': payment['delivery_payer_type'],
        'payment_method': payment.get('delivery_payment_method', 'Cash'),
        'cod_enabled': payment.get('cod_enabled', payment['cod_amount_value'] > 0),
        'paid_amount': payment['paid_amount'],
    }
    return {
        'items': items,
        'total': Decimal(order.total_sum or 0),
        'delivery': _delivery_text(order),
        'payment': (order.pay_type or '', order.payment_status or ''),
        'shipment_payment': shipment_payment,
        'delivery_method': getattr(order, 'delivery_method', 'nova_poshta'),
        'full_name': (order.full_name or '').strip(),
        'phone': (order.phone or '').strip(),
    }


def _index_by_key(items):
    """Групує позиції за ключем (на випадок дублів — підсумовуємо кількість)."""
    index = {}
    for it in items:
        existing = index.get(it['key'])
        if existing is None:
            index[it['key']] = dict(it)
        else:
            existing['qty'] += it['qty']
            if it.get('item_kind') == 'dtf_film':
                existing['film_length_m'] += it['film_length_m']
    return index


def build_order_edit_diff(before, after):
    """Порівнює два знімки замовлення і повертає структурований diff.

    Повертає dict::

        {
          'items': {'added': [...], 'removed': [...], 'changed': [...]},
          'total': {'old', 'new', 'delta'} | None,
          'delivery': {'old', 'new'} | None,
          'payment': {'old', 'new'} | None,   # (pay_type, payment_status)
          'customer': {'old', 'new'} | None,
          'has_changes': bool,
        }
    """
    before_index = _index_by_key(before.get('items', []))
    after_index = _index_by_key(after.get('items', []))

    added, removed, changed = [], [], []

    for key, item in after_index.items():
        if key not in before_index:
            added.append({'label': item['label'], 'qty': item['qty'], 'unit_price': item['unit_price'],
                          **({field: item[field] for field in ('item_kind', 'film_length_m')} if item.get('item_kind') == 'dtf_film' else {})})

    for key, item in before_index.items():
        if key not in after_index:
            removed.append({'label': item['label'], 'qty': item['qty'], 'unit_price': item['unit_price'],
                            **({field: item[field] for field in ('item_kind', 'film_length_m')} if item.get('item_kind') == 'dtf_film' else {})})

    for key, before_item in before_index.items():
        after_item = after_index.get(key)
        if after_item is None:
            continue
        qty_changed = before_item['qty'] != after_item['qty']
        price_changed = before_item['unit_price'] != after_item['unit_price']
        length_changed = before_item.get('film_length_m') != after_item.get('film_length_m')
        if qty_changed or price_changed or length_changed:
            changed.append({
                'label': after_item['label'],
                'old_qty': before_item['qty'],
                'new_qty': after_item['qty'],
                'old_price': before_item['unit_price'],
                'new_price': after_item['unit_price'],
                **({
                    'item_kind': 'dtf_film',
                    'old_film_length_m': before_item.get('film_length_m'),
                    'new_film_length_m': after_item.get('film_length_m'),
                } if after_item.get('item_kind') == 'dtf_film' else {}),
            })

    total_diff = None
    if before.get('total') != after.get('total'):
        old_total = Decimal(before.get('total') or 0)
        new_total = Decimal(after.get('total') or 0)
        total_diff = {'old': old_total, 'new': new_total, 'delta': new_total - old_total}

    delivery_diff = None
    if (before.get('delivery') or '') != (after.get('delivery') or '') or before.get('delivery_method') != after.get('delivery_method'):
        delivery_diff = {'old': before.get('delivery') or '', 'new': after.get('delivery') or ''}

    payment_diff = None
    if before.get('payment') != after.get('payment') or before.get('shipment_payment') != after.get('shipment_payment'):
        payment_diff = {'old': before.get('payment'), 'new': after.get('payment'),
                        'old_shipment_payment': before.get('shipment_payment'),
                        'new_shipment_payment': after.get('shipment_payment')}

    customer_diff = None
    before_customer = (before.get('full_name') or '', before.get('phone') or '')
    after_customer = (after.get('full_name') or '', after.get('phone') or '')
    if before_customer != after_customer:
        customer_diff = {'old': before_customer, 'new': after_customer}

    has_changes = bool(
        added or removed or changed
        or total_diff or delivery_diff or payment_diff or customer_diff
    )

    return {
        'items': {'added': added, 'removed': removed, 'changed': changed},
        'total': total_diff,
        'delivery': delivery_diff,
        'payment': payment_diff,
        'customer': customer_diff,
        'has_changes': has_changes,
    }
