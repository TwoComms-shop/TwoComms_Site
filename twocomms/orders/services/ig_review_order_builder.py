"""Materialize complete accepted Instagram orders after an audited approval."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from types import SimpleNamespace


def _money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount > 0 and amount == amount.quantize(Decimal('0.01')):
            return amount.quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def _completion(*fields):
    return {'status': 'needs_manual_completion', 'order': None, 'missing_fields': sorted(set(fields))}


def _current_agreement_error(client, draft):
    """Reproduce owned sources under the current conversation fence."""
    from management.models import InstagramBotMessage, InstagramBotSettings
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversation_agreement import (
        MAX_MESSAGES, _row, extract_conversation_agreement, read_conversation_agreement,
    )
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_memory_producer import _namespaces, _source_allowed

    stored = (client.sales_context or {}).get('conversation_agreement') if isinstance(client.sales_context, dict) else None
    if not isinstance(stored, dict) or not isinstance(stored.get('scope'), dict):
        return 'conversation_agreement_unavailable'
    scope = stored['scope']
    current_floor = conversation_route_reset_floor(client.pk)
    namespace = scope.get('source_namespace') or ''
    settings = InstagramBotSettings.objects.order_by('pk').first()
    if settings is None or namespace != ingress_provider_namespace(settings):
        return 'conversation_agreement_current_namespace_changed'
    captured = read_conversation_agreement(
        client, episode_id=client.current_commercial_episode_id,
        source_namespace=namespace, reset_floor=current_floor,
    )
    agreement = captured.get('agreement') or {}
    if not agreement:
        return captured.get('reason') or 'conversation_agreement_unavailable'
    if any(reason in (agreement.get('uncertainty_reasons') or []) for reason in ('quote_expiry_unknown', 'quote_expiry_timezone_unknown')):
        return 'conversation_agreement_quote_expiry_unproven'
    recorded = draft.get('agreement')
    if not isinstance(recorded, dict):
        return 'conversation_agreement_draft_unproven'
    for key in ('items', 'amounts', 'shipping', 'payment_instruction', 'packaging', 'evidence'):
        if recorded.get(key) != agreement.get(key):
            return 'conversation_agreement_draft_changed'
    for key in ('items', 'merchandise_total', 'delivery_amount', 'payable_total'):
        if draft.get(key) != agreement.get(key):
            return 'conversation_agreement_draft_changed'
    shipping = agreement.get('shipping') or {}
    delivery = draft.get('delivery') or {}
    from orders.nova_poshta_documents import normalize_checkout_phone
    for key in ('full_name', 'phone', 'city', 'office'):
        values = [str(container.get(key) or '') for container in (delivery, shipping)]
        if key == 'phone':
            values = [normalize_checkout_phone(value) for value in values]
        else:
            values = [' '.join(value.replace('№', ' ').replace('#', ' ').split()).casefold() for value in values]
        if values[0] != values[1]:
            return 'conversation_agreement_delivery_changed'
    from django.utils import timezone
    # The reader has reverified the retained material transcript. Preserve
    # those rows rather than replacing them with a window of recent noise.
    retained_rows = captured.get('source_rows') or []
    new_sources = list(InstagramBotMessage.objects.filter(
        client_id=client.pk, pk__gt=agreement['watermark_message_id'], pk__gte=current_floor,
    ).order_by('pk')[:MAX_MESSAGES + 1])
    if len(retained_rows) + len(new_sources) > MAX_MESSAGES:
        return 'conversation_agreement_current_sources_overflow'
    namespaces = _namespaces(new_sources)
    now = timezone.now()
    if any(
        source.sender_id != client.igsid or namespaces.get(source.pk) != namespace
        or not _source_allowed(source) or (source.provider_created_at or source.created_at) > now
        for source in new_sources
    ):
        return 'conversation_agreement_current_namespace_changed'
    reproduced = extract_conversation_agreement([
        *retained_rows,
        *[{**_row(source), 'provider_namespace': namespaces[source.pk]} for source in new_sources],
    ])
    for key in ('items', 'amounts', 'shipping', 'payment_instruction', 'packaging'):
        if reproduced.get(key) != agreement.get(key):
            return 'conversation_agreement_current_sources_changed'
    return ''


def _configuration_correction_error(client, draft, *, lock=False):
    """An audited canonical correction cannot be bypassed by an older offer."""
    from management.models import IgCommerceSelectionSession, IgCommerceSelectionTransition
    from management.services.ig_commerce_projection import source_preferences_for
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_commerce_turns import _canonical_source_size

    sessions = IgCommerceSelectionSession.objects.filter(client_id=client.pk, open_slot=1)
    if lock:
        sessions = sessions.select_for_update()
    sessions = list(sessions.order_by('-generation'))
    corrections = IgCommerceSelectionTransition.objects.filter(
        session_id__in=[session.pk for session in sessions], action='manager_size_correction',
    )
    if not corrections.exists():
        return ''
    if len(sessions) != 1:
        return 'configuration_correction'
    session = sessions[0]
    if (
        session.state != session.State.OPEN
        or session.commercial_episode_id != client.current_commercial_episode_id
        or corrections.filter(to_revision__gt=session.revision).exists()
    ):
        return 'configuration_correction'
    lines = session.lines if isinstance(session.lines, list) else []
    items = draft.get('items') if isinstance(draft.get('items'), list) else []
    # A source correction is tied to one recipient/line. An order with several
    # lines needs an explicit mapping; never apply an active size to every item.
    if len(lines) != 1 or len(items) != 1 or session.active_index != 0:
        return 'configuration_correction'
    line, item = lines[0], items[0]
    if not isinstance(line, dict) or not isinstance(item, dict) or not line.get('line_id'):
        return 'configuration_correction'
    if str(line.get('recipient_id') or 'self') != str(item.get('recipient_id') or 'self'):
        return 'configuration_correction'
    if item.get('line_id') and item['line_id'] != line['line_id']:
        return 'configuration_correction'
    if str(line.get('product_id') or '') != str(item.get('product_id') or ''):
        return 'configuration_correction'
    projection = source_preferences_for(
        client, episode_id=client.current_commercial_episode_id, line_id=line['line_id'],
    )
    expected = {
        'session_id': session.pk, 'generation': session.generation, 'revision': session.revision,
        'active_index': 0, 'episode_id': client.current_commercial_episode_id,
        'line_id': line['line_id'], 'recipient_id': str(line.get('recipient_id') or 'self'),
        'reset_floor': conversation_route_reset_floor(client.pk),
    }
    if not projection or any(projection.get(key) != value for key, value in expected.items()):
        return 'configuration_correction'
    if (projection.get('cleared') or {}).get('size'):
        return 'configuration_correction'
    size = (projection.get('values') or {}).get('size')
    proof = (projection.get('evidence') or {}).get('size') or {}
    if not size or not proof:
        return 'configuration_correction'
    if _canonical_source_size(str(size)) != _canonical_source_size(str(item.get('size') or '')):
        return 'configuration_correction'
    return ''


def _review_order_operation(review, *, actor=None, create=True):
    """No order from a receipt alone; missing source facts leave a manual task.

    The payment decision, accepted item configuration, quote and delivery are
    independent requirements. This function performs no customer/provider send.
    """
    from management.ig_bot_models import IgClient, IgPaymentConfirmationReview, IgPaymentProjection
    from management.services.ig_commercial_episodes import ensure_episode_for_review, payment_truth_snapshot
    from management.services.ig_order_links import authoritative_manager_decision, create_order_attribution
    from orders.models import Order, OrderItem
    from orders.nova_poshta_documents import normalize_checkout_phone
    from orders.services.order_builder import assert_order_matches_commercial_contract
    from storefront.views.manual_orders import _build_order_item, _collect_items, _review_delivery_contract

    if create:
        from management.services.ig_payment_review import _lock_payment_review
        locked = _lock_payment_review(review)
    else:
        locked = IgPaymentConfirmationReview.objects.select_related('client', 'deal', 'order').get(pk=review.pk)
    if locked.client.hidden_at:
        return _completion('visible_client')
    decision = authoritative_manager_decision(locked)
    if decision is None:
        return _completion('manager_payment_decision')
    if actor is not None and not (getattr(actor, 'is_staff', False) or getattr(actor, 'is_superuser', False)):
        return _completion('authenticated_manager')
    if actor is not None and str(decision.actor_external_id) != str(actor.pk):
        return _completion('decision_actor')
    projection = None
    if locked.deal_id:
        projections = IgPaymentProjection.objects
        if create:
            projections = projections.select_for_update()
        projection = projections.filter(deal_id=locked.deal_id).first()
    truth = payment_truth_snapshot(review=locked, projection=projection, decision=decision)
    if truth['needs_reconciliation']:
        return _completion('payment_reconciliation')
    if locked.order_id:
        if create:
            review.order_id = locked.order_id
        return {'status': 'already_created', 'order': locked.order, 'missing_fields': []}
    evidence = locked.evidence if isinstance(locked.evidence, dict) else {}
    draft = evidence.get('order_draft') if isinstance(evidence.get('order_draft'), dict) else {}
    correction_error = _configuration_correction_error(locked.client, draft, lock=create)
    if correction_error:
        return _completion(correction_error)
    agreement_error = _current_agreement_error(locked.client, draft)
    if agreement_error:
        return _completion(agreement_error)
    delivery = draft.get('delivery') if isinstance(draft.get('delivery'), dict) else {}
    missing = []
    name = str(delivery.get('full_name') or '').strip()
    phone = normalize_checkout_phone(str(delivery.get('phone') or ''))
    city = str(delivery.get('city') or '').strip()
    office = str(delivery.get('office') or delivery.get('np_office') or '').strip()
    for field, value in [('delivery.full_name', name), ('delivery.phone', phone), ('delivery.city', city), ('delivery.office', office)]:
        if not value:
            missing.append(field)
    merchandise = _money(draft.get('merchandise_total') or draft.get('quoted_total'))
    payable = _money(draft.get('payable_total') or draft.get('quoted_total'))
    raw_delivery = draft.get('delivery_amount') or draft.get('delivery_total') or '0'
    try:
        delivery_amount = Decimal(str(raw_delivery))
        if not delivery_amount.is_finite() or delivery_amount < 0:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        delivery_amount = None
    if merchandise is None or payable is None or delivery_amount is None or merchandise + delivery_amount != payable:
        missing.append('commercial_amounts')
    if payable is not None and (
        decision.verification_scope == 'full_payment' and Decimal(decision.confirmed_amount) != payable
        or decision.verification_scope == 'prepayment' and not 0 < Decimal(decision.confirmed_amount) < payable
    ):
        missing.append('confirmed_amount_scope')
    raw_items = draft.get('items') if isinstance(draft.get('items'), list) else []
    if not raw_items:
        missing.append('items')
    prepared = []
    for index, item in enumerate(raw_items):
        prefix = f'items.{index}'
        if not isinstance(item, dict):
            missing.append(prefix)
            continue
        if item.get('authority') != 'conversation_agreement' or item.get('configuration_authority') != 'customer_confirmed_seller_offer':
            missing.append(prefix + '.accepted_agreement')
        if not str(item.get('acceptance_message_id') or '').isdigit() or not str(item.get('source_message_id') or '').isdigit():
            missing.append(prefix + '.source_messages')
        garment = str(item.get('garment_type') or '').strip()
        title = str(item.get('title') or '').strip()
        fit = str(item.get('fit_option_code') or item.get('fit') or '').strip()
        size = str(item.get('size') or '').strip()
        color = str(item.get('color_name') or item.get('color') or '').strip()
        for key, value in [('garment_type', garment), ('title', title), ('fit', fit), ('size', size), ('color', color)]:
            if not value or value == 'unknown':
                missing.append(prefix + '.' + key)
        qty = item.get('qty')
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 50:
            missing.append(prefix + '.qty')
        price = _money(item.get('unit_price'))
        if price is None:
            missing.append(prefix + '.unit_price')
        product_id = item.get('product_id')
        if product_id and not item.get('color_variant_id'):
            missing.append(prefix + '.catalog_color_variant')
        elif not product_id and (item.get('identity_kind') != 'offsite_named' or item.get('identity_status') != 'customer_confirmed_source'):
            missing.append(prefix + '.custom_identity')
        options = dict(item.get('option_values') or {}) if isinstance(item.get('option_values'), dict) else {}
        labels = dict(item.get('option_labels') or {}) if isinstance(item.get('option_labels'), dict) else {}
        options['garment_type'] = garment
        labels.setdefault('Тип', {'tshirt': 'Футболка', 'hoodie': 'Худі', 'sweatshirt': 'Світшот', 'longsleeve': 'Лонгслів'}.get(garment, garment))
        references = {
            int(value) for value in (item.get('reference_message_ids') or [])
            if str(value).isdigit()
        }
        references.update(int(item[key]) for key in ('source_message_id', 'acceptance_message_id') if str(item.get(key) or '').isdigit())
        options['_reference_message_ids'] = sorted(references)
        prepared.append({
            'kind': 'catalog' if product_id else 'custom', 'product_id': product_id,
            'color_variant_id': item.get('color_variant_id'),
            'title': title, 'color_name': color, 'size': size,
            'fit_option_code': fit, 'fit_option_label': str(item.get('fit_option_label') or {'oversize': 'Оверсайз', 'classic': 'Класична'}.get(fit, fit)),
            'option_values': options, 'option_labels': labels,
            'qty': qty, 'unit_price': price,
        })
    if missing:
        return _completion(*missing)
    provisional = Order()
    try:
        _raw_items, products_map, variants_map = _collect_items(prepared)
        items = [_build_order_item(item, order=provisional, products_map=products_map, variants_map=variants_map) for item in prepared]
    except ValueError:
        return _completion('catalog_configuration')
    if any(item.fit_option_code != raw['fit_option_code'] for item, raw in zip(items, prepared)):
        return _completion('catalog_fit_snapshot')
    if sum((item.line_total for item in items), Decimal('0')) != merchandise:
        return _completion('item_total')
    if not str(decision.actor_external_id or '').isdigit():
        return _completion('manager_actor_identity')
    audited_actor = actor or decision.actor or SimpleNamespace(pk=int(decision.actor_external_id))
    try:
        shipping = _review_delivery_contract(locked, merchandise_total=merchandise, actor=audited_actor)
    except ValueError:
        return _completion('delivery_amount_source')
    if locked.deal_id:
        from management.services.ig_payment_review import _is_review_deal_compatible

        if not _is_review_deal_compatible(locked.deal, {item.product_id for item in items if item.product_id}):
            return _completion('existing_deal_configuration')
    if not create:
        return {'status': 'ready', 'order': None, 'missing_fields': []}
    # Recheck under the client/session fence immediately before the first
    # materialization effect; a correction waiter cannot race this boundary.
    correction_error = _configuration_correction_error(locked.client, draft, lock=True)
    if correction_error:
        return _completion(correction_error)
    episode = ensure_episode_for_review(locked)
    payload = {
        'manual_payment_preset': 'manager_prepayment' if decision.verification_scope == 'prepayment' else 'unpaid_full',
        'instagram_payment_review_id': locked.pk,
        'instagram_commercial_episode_id': episode.pk,
        'manual_payment_evidence_confirmed': True,
        'provider_payment_confirmed': False,
        'manager_payment_decision_id': decision.pk,
        'manager_confirmed_amount': f'{Decimal(decision.confirmed_amount):.2f}',
        'manager_verification_scope': decision.verification_scope,
        'manager_verification_source': decision.verification_source,
        'manager_amount_source': decision.amount_source or '',
        'manager_amount_evidence_message_ids': decision.amount_evidence_message_ids or [],
        'manager_payment_currency': decision.currency,
        'effective_confirmed_amount': f'{Decimal(decision.confirmed_amount):.2f}',
        'negotiated_order_total': f'{payable:.2f}',
        **({'instagram_delivery_contract': shipping} if shipping else {}),
    }
    fields = {'full_name': name[:200], 'phone': phone, 'city': city[:100], 'np_office': office[:200]}
    order, created = Order.objects.get_or_create(
        checkout_idempotency_key=f'ig-episode:{episode.pk}',
        defaults={
            **fields, 'source': 'manual', 'sale_source': 'Instagram', 'status': 'new',
            'payment_status': 'unpaid', 'payment_provider': '',
            'pay_type': 'prepayment' if decision.verification_scope == 'prepayment' else 'online_full',
            'created_by': actor or decision.actor, 'total_sum': merchandise, 'payment_payload': payload,
        },
    )
    if not created:
        try:
            assert_order_matches_commercial_contract(
                order, expected_fields=fields, expected_items=items,
                declared_total=merchandise, expected_delivery_contract=shipping,
            )
        except ValueError:
            return _completion('episode_order_contract')
    else:
        for item in items:
            item.order = order
        OrderItem.objects.bulk_create(items)
    locked.order = order
    locked.save(update_fields=['order', 'updated_at'])
    if locked.deal_id:
        from django.utils import timezone
        deal = locked.deal.__class__.objects.select_for_update().get(pk=locked.deal_id)
        if deal.order_id not in {None, order.pk}:
            raise ValueError('Угоду вже прив’язано до іншого замовлення.')
        deal.order = order
        deal.status = deal.Status.ORDER_CREATED
        deal.order_truth_updated_at = timezone.now()
        deal.save(update_fields=['order', 'status', 'order_truth_updated_at', 'updated_at'])
    create_order_attribution(
        order, client=locked.client, deal=locked.deal, review=locked, manager_decision=decision,
        creation_mode='manager_review', payment_source='manager_verified',
        created_by=actor or decision.actor,
    )
    review.order_id = order.pk
    return {'status': 'created' if created else 'already_created', 'order': order, 'missing_fields': []}


def create_order_from_payment_review(review, *, actor=None):
    """Enter the episode barrier before client/review/session database locks."""
    from management.services.ig_payment_review import _payment_review_mutation
    with _payment_review_mutation(review):
        return _review_order_operation(review, actor=actor, create=True)


def payment_review_order_completion_requirements(review):
    """SELECT-only projection of the same approval requirements for the UI."""
    return _review_order_operation(review, create=False)
