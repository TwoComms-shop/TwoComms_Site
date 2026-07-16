"""Shared payment-type contract for profiles and orders."""

PAY_TYPE_CHOICES = (
    ('online_full', 'Онлайн оплата (повна сума)'),
    ('prepay_200', 'Передплата 200 грн'),
    ('cod', 'Оплата при отриманні'),
)

_ALIASES = {
    'online_full': 'online_full',
    'online': 'online_full',
    'online-full': 'online_full',
    'online_full_payment': 'online_full',
    'full': 'online_full',
    'онлайн оплата (повна сума)': 'online_full',
    'оплата повністю': 'online_full',
    'оплатити повністю': 'online_full',
    'prepay_200': 'prepay_200',
    'prepay': 'prepay_200',
    'prepay200': 'prepay_200',
    'prepaid': 'prepay_200',
    'partial': 'prepay_200',
    'partial_payment': 'prepay_200',
    'cod': 'cod',
    'cash': 'cod',
    'cash_on_delivery': 'cod',
    'внести передплату 200 грн': 'prepay_200',
    'передплата 200 грн': 'prepay_200',
    'передплата 200 грн (решта при отриманні)': 'prepay_200',
}


def normalize_pay_type(value, default='online_full'):
    """Return a supported canonical payment type before any model save."""
    key = str(value or '').strip().lower()
    canonical = _ALIASES.get(key)
    if canonical:
        return canonical
    if default is None:
        raise ValueError(f'Unsupported payment type: {value!r}')
    return default
