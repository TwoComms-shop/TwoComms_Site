"""Human-facing selection fields, separate from checkout permission gates."""

from datetime import datetime


GARMENT_LABELS = {'tshirt': 'Футболка', 'hoodie': 'Худі', 'longsleeve': 'Лонгслів',
                  'sweatshirt': 'Світшот', 'pants': 'Штани', 'shorts': 'Шорти'}


def _checkout_ref(selection, key, field):
    """Display exact typed fields from the caller's validated native capture.

    The canonical adapter owns receipt verification and capture-clock fences.
    This pure display adapter neither discovers receipts nor grants actions.
    """
    source, scope = field.get('source'), selection.get('scope')
    if (selection.get('schema') != 'source-selection.v1' or not isinstance(scope, dict)
        or any(type(scope.get(name)) is not int or scope[name] <= 0 for name in ('client_id', 'session_id'))
        or not isinstance(selection.get('evidence'), dict) or not isinstance(selection.get('values'), dict)
        or not isinstance(source, dict) or field.get('authority') != 'checkout_link_holder'
        or source != (selection.get('evidence') or {}).get(key)
        or field.get('value') != (selection.get('values') or {}).get(key)
        or field.get('status') not in {'confirmed', 'ambiguous'}):
        return None
    try:
        observed = datetime.fromisoformat(str(source.get('observed_at')).replace('Z', '+00:00'))
        from management.services.ig_turn_intelligence import _checkout_holder_proof
        if observed.tzinfo is None or not _checkout_holder_proof(source, seal_at=observed):
            return None
    except (TypeError, ValueError):
        return None
    return {'kind': 'checkout_cart_amendment', 'id': source['transition_id'],
        'amendment_id': source['amendment_id'], 'digest': source['source_digest'],
        'authority': 'checkout_link_holder', 'event_at': source['observed_at']}


def source_selection_fields(selection):
    """Partial customer choice remains visible before catalogue applicability."""
    if (not isinstance(selection, dict) or not isinstance(selection.get('scope'), dict)
        or not isinstance(selection.get('fields'), dict) or not selection['fields']):
        return None
    labels = {'garment_type': 'Тип речі', 'model_query': 'Товар / принт', 'product_id': 'Товар каталогу',
              'fit_option_code': 'Посадка', 'color': 'Колір', 'size': 'Розмір',
              'quantity': 'Кількість', 'purchase_requested': 'Намір замовити'}
    keys = {'garment_type': 'kind', 'model_query': 'product', 'product_id': 'product', 'fit_option_code': 'fit'}
    rows = []
    for key, field in selection['fields'].items():
        if key not in labels or not isinstance(field, dict) or not isinstance(field.get('source'), dict):
            continue
        if key == 'model_query' and 'product_id' in selection['fields']:
            continue
        source = field['source']
        native = field.get('authority') == 'checkout_link_holder'
        if native:
            ref = _checkout_ref(selection, key, field)
            if ref is None:
                continue
        else:
            identity = source.get('source_message_id')
            if type(identity) is not int or identity <= 0:
                continue
            ref = {'kind': 'message', 'id': identity}
        if 'value' not in field or 'status' not in field:
            continue
        rows.append({'key': keys.get(key, key), 'label': labels[key],
                     'value': (('Так' if field['value'] else 'Ні') if key == 'purchase_requested' else
                               f"#{field['value']}" if key == 'product_id' else
                               GARMENT_LABELS.get(field['value'], str(field['value'])) if key == 'garment_type' else str(field['value'])),
                     'required': False, 'status': 'partial', 'choice_status': field['status'],
                     'applicability': 'unknown', 'availability': 'unknown',
                     'evidence_refs': [ref],
                     'source': source,
                     'note': ('Змінено за посиланням оформлення. ' if native else 'Побажання клієнта. ')
                         + 'Застосовність і наявність ще не підтверджено.'})
    if not rows:
        return None
    return {'schema': 'journey-selection.v1', 'label': 'Поточний вибір · зміни в оформленні' if any(
                row['evidence_refs'][0]['kind'] == 'checkout_cart_amendment' for row in rows) else 'Побажання клієнта',
            'completed': 0, 'total': None, 'items': rows,
            'scope': selection['scope'],
            'evidence_refs': list({(row['evidence_refs'][0]['kind'], row['evidence_refs'][0]['id'],
                row['evidence_refs'][0].get('amendment_id')): row['evidence_refs'][0] for row in rows}.values()),
            'note': 'Повний набір вимог уточнюється після вибору товару. Намір замовити не є оформленим замовленням.'}


def selection_fields(readiness, *, scope, evidence_refs, source_selection=None):
    if not evidence_refs or not readiness.get('has_product'):
        return source_selection_fields(source_selection)
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
    partial = source_selection_fields(source_selection)
    if partial:
        by_key = {row['key']: row for row in rows}
        for choice in partial['items']:
            row = by_key.get(choice['key'])
            if row is None:
                rows.append(choice)
                continue
            row.update({key: choice[key] for key in ('source', 'evidence_refs', 'choice_status')})
            row['choice_value'] = choice['value']
            if not row['value']:
                row['value'] = choice['value']
                row['note'] = choice['note']
                # Displaying source choice must not complete a catalogue gate.
            row['applicability'] = ('applicable' if row['required'] else 'not_applicable') if known else 'unknown'
            row['availability'] = ('unavailable' if unavailable else 'available' if size.get('selected') else 'unknown') if row['key'] == 'size' else 'unknown'
    required = [r for r in rows if r['required']]
    return {'schema': 'journey-selection.v1', 'label': 'Параметри товару',
            'completed': sum(r['status'] == 'complete' for r in required),
            'total': len(required) if known else None, 'items': rows,
            'scope': scope, 'evidence_refs': evidence_refs,
            'note': 'Готовність параметрів не є дозволом на оплату.' if known else 'Повний набір вимог уточнюється після вибору товару, посадки та кольору.'}
