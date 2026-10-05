"""Bounded read-only basket projection. No historical, legacy or payment authority.

Distinct line IDs stay distinct, including identical items. Quantity is proven
alongside configuration; absent quantity stays unknown, never an inferred one.
"""
from copy import deepcopy
from decimal import Decimal

from django.db import connection
from management.models import IgCommerceSelectionTransition
from management.services import ig_journey_readiness as scope_reader
from management.services.ig_journey_selection import selection_fields
from storefront.services.fact_registry import free_shipping_threshold


class _CartBudget(scope_reader._ReadBudget):
    def __call__(self, execute, sql, params, many, context):
        if self.reads >= 160:
            self.exceeded = True
            raise scope_reader._CatalogBudgetExceeded('cart_read_budget')
        if not sql.lstrip().upper().startswith('SELECT'):
            raise scope_reader._CatalogBudgetExceeded('cart_not_read_only')
        self.reads += 1
        return execute(sql, params, many, context)


def _price(readiness, fields):
    if not fields or fields['total'] is None or fields['completed'] != fields['total']:
        return None
    from storefront.models import Product, ProductStatus
    from product_catalog.services import effective_cart_unit_price
    product = Product.objects.filter(pk=readiness['product']['id'], status=ProductStatus.PUBLISHED).first()
    if product is None:
        return None
    variant_id = (readiness.get('color') or {}).get('selected_variant_id')
    colors = (readiness.get('color') or {}).get('options') or []
    if not variant_id and len(colors) == 1:
        variant_id = colors[0].get('variant_id')
    variant = product.color_variants.filter(pk=variant_id).first() if variant_id else None
    if variant_id and variant is None:
        return None
    options = {a['code']: a['selected'] for a in (readiness.get('options') or {}).get('axes', []) if a.get('selected')}
    price = Decimal(str(effective_cart_unit_price(product, variant,
        fit_code=(readiness.get('fit') or {}).get('selected', ''), option_values=options)))
    return str(price.quantize(Decimal('.01'))) if price.is_finite() and price > 0 else None


def summarize_cart(lines, *, scope):
    quantities_known = all(isinstance(row.get('quantity'), int) and row['quantity'] > 0 for row in lines)
    exact = quantities_known and all(row.get('subtotal') is not None for row in lines)
    total = sum((Decimal(row['subtotal']) for row in lines), Decimal(0)) if exact else None
    threshold = free_shipping_threshold()
    eligible = total is not None and total >= threshold
    return {'schema': 'journey-cart.v1', 'scope': scope, 'lines': lines,
            'line_count': len(lines), 'item_count': sum(row['quantity'] for row in lines) if quantities_known else None,
            'ready_count': sum(bool(row.get('fields') and row['fields']['total'] is not None and row['fields']['completed'] == row['fields']['total']) for row in lines),
            'currency': 'UAH', 'estimated_total': str(total) if total is not None else None,
            'shipping': {'threshold': str(threshold), 'eligible_estimate': eligible,
                         'remaining': str(max(Decimal(0), threshold-total)) if total is not None else None},
            'note': 'Попередня сума за поточними цінами каталогу, без промокодів. Остаточні умови — у замовленні.'}


def selection_cart(*, client_id, episode_id):
    first = scope_reader._client_fence(client_id)
    if not first or first['privacy_erasure_started_at'] or not episode_id or first['current_commercial_episode_id'] != episode_id:
        return None
    floor = scope_reader.conversation_route_reset_floor(client_id)
    session = scope_reader._session(client_id, episode_id)
    if session is None:
        return None
    snapshot = deepcopy(session.snapshot())
    fence = scope_reader._session_fence(session)
    lines = snapshot.get('lines')
    if not isinstance(lines, list) or not 0 < len(lines) <= scope_reader.LINE_LIMIT:
        return None
    if any(not isinstance(line, dict) or not line.get('line_id') for line in lines) or len({line['line_id'] for line in lines}) != len(lines):
        return None
    rows = list(IgCommerceSelectionTransition.objects.filter(session_id=session.pk, to_revision__lte=session.revision)
                .order_by('-to_revision').values(*scope_reader._SOURCE_TRANSITION_FIELDS)[:scope_reader.TRANSITION_LIMIT])
    budget, cache, output, refs = _CartBudget(), {}, [], []
    for position, line in enumerate(lines):
        if any(key in line and line[key] != expected for key, expected in (('client_id', client_id), ('commercial_episode_id', episode_id), ('session_id', session.pk))):
            return None
        evidence = scope_reader._owned_evidence(session, snapshot, line, floor, rows=rows, include_quantity=True)
        row = {'line_id': line['line_id'], 'position': position+1, 'active': position == snapshot.get('active_index'),
               'quantity': None, 'fields': None, 'unit_price': None, 'subtotal': None, 'evidence_refs': evidence or []}
        if evidence:
            refs.extend(evidence)
            quantity = line.get('quantity')
            if isinstance(quantity, int) and not isinstance(quantity, bool) and 0 < quantity <= 9999:
                row['quantity'] = quantity
            key = scope_reader._digest({k: line.get(k) for k in scope_reader._SELECTION_KEYS})
            try:
                if key not in cache:
                    with connection.execute_wrapper(budget):
                        readiness = scope_reader.selection_readiness(product_id=line.get('product_id'), selection=line,
                            size=line.get('size') or '', quantity=1, strict=True)
                        fields = selection_fields(readiness, scope={}, evidence_refs=evidence)
                        price = _price(readiness, fields)
                    cache[key] = (fields, price)
                fields, price = deepcopy(cache[key])
                if fields:
                    fields['scope'] = {'line_id': line['line_id'], 'position': position+1}
                    fields['evidence_refs'] = evidence
                    fields['items'] = [item for item in fields['items'] if item['key'] != 'quantity']
                row.update(fields=fields, unit_price=price)
                if price is not None and row['quantity'] is not None:
                    row['subtotal'] = str(Decimal(price)*row['quantity'])
            except Exception:
                row['reason'] = 'catalog_read_budget' if budget.exceeded else 'catalog_unavailable'
        else:
            row['reason'] = 'no_owned_current_source'
        output.append(row)
    last_session = scope_reader._session(client_id, episode_id)
    if scope_reader._client_fence(client_id) != first or scope_reader.conversation_route_reset_floor(client_id) != floor or last_session is None or scope_reader._session_fence(last_session) != fence:
        return None
    if not refs or not scope_reader._evidence_sources_current(refs, client_id=client_id, reset_floor=floor):
        return None
    return summarize_cart(output, scope={'client_id': client_id, 'episode_id': episode_id,
        'session_id': session.pk, 'revision': session.revision, 'snapshot_digest': fence[-1]})
