"""Human-facing selection fields, separate from checkout permission gates."""


def selection_fields(readiness, *, scope, evidence_refs):
    if not evidence_refs or not readiness.get('has_product'):
        return None
    product = readiness.get('product') or {}
    fit, color, size, options = (readiness.get(k) or {} for k in ('fit', 'color', 'size', 'options'))
    known = readiness.get('applicability_known') is True and not options.get('error')
    rows = []

    def add(key, label, value='', *, required=True, status=None, note=''):
        rows.append({'key': key, 'label': label, 'value': str(value or ''), 'required': required,
                     'status': status or ('complete' if value else 'open'), 'note': note})

    add('kind', 'Тип речі', product.get('kind', ''), note='Тип визначається товаром каталогу.')
    add('product', 'Товар / принт', product.get('title', ''))
    fit_value = next((r.get('label') or r.get('code') for r in fit.get('options', []) if r.get('code') == fit.get('selected')), fit.get('selected', ''))
    add('fit', 'Посадка', fit_value, required=bool(fit.get('required')), note='' if fit.get('required') else 'Для цього товару окремий вибір не потрібен.')
    colors = color.get('options') or []
    color_value = color.get('selected', '') if color.get('selected_variant_id') else ''
    if len(colors) == 1:
        color_value = colors[0].get('name', '')
    add('color', 'Колір', color_value, required=bool(color.get('required') or colors), note='Єдиний варіант у каталозі.' if len(colors) == 1 else '')
    unavailable = size.get('requested_unavailable', '')
    add('size', 'Розмір', size.get('selected') or unavailable or '', required=bool(size.get('required')), status='invalidated' if unavailable else None, note='Зараз немає в наявності; потрібна альтернатива або очікування.' if unavailable else '')
    for axis in options.get('axes') or []:
        code = axis.get('code')
        if not code or code == 'fit':
            continue
        selected = axis.get('selected') or ''
        choices = axis.get('choices') or []
        value = next((c.get('label') or c.get('code') for c in choices if c.get('code') == selected and c.get('is_enabled')), '')
        if not value and axis.get('is_fixed'):
            enabled = [c for c in choices if c.get('is_enabled')]
            if len(enabled) == 1:
                value = enabled[0].get('label') or enabled[0].get('code')
        add('option:' + code, axis.get('label') or code, value)
    # Quantity defaults to one in checkout. It is context, not an extra
    # mandatory customer answer or evidence of an explicit quantity request.
    add('quantity', 'Кількість', readiness.get('quantity') or 1, required=False,
        note='Значення комплектації; може бути типовим 1, не окрема підтверджена відповідь клієнта.')
    required = [r for r in rows if r['required']]
    return {'schema': 'journey-selection.v1', 'label': 'Параметри товару',
            'completed': sum(r['status'] == 'complete' for r in required),
            'total': len(required) if known else None, 'items': rows,
            'scope': scope, 'evidence_refs': evidence_refs,
            'note': 'Готовність параметрів не є дозволом на оплату.' if known else 'Повний набір вимог уточнюється після вибору товару, посадки та кольору.'}
