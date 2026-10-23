"""Read-only navigation hints for existing native configuration review rows.

These hints grant no continuation, stock, checkout or message-send authority.
"""
from management.services.ig_revision_outbox import _digest, revision_sources_current
from management.services.ig_revision_intents import product_review_receipt_reason


def manager_review_projections(client, tasks, *, reset_floor=None):
    from management.models import (
        InstagramBotSettings, IgCustomerTurnRevision, IgTurnRevisionSource,
        IgBotNotification, IgConversationRouteDecision,
    )
    from management.services.instagram_bot import ingress_provider_namespace
    from management.services.ig_conversation_routes import conversation_route_reset_floor
    from management.services.ig_commerce_projection import capture_current_selection_lines
    from management.services.ig_product_inquiries import cart_identity, product_inquiries_for_revision
    from management.services.ig_turn_intent import _accepted_scope

    if client.privacy_erasure_started_at or client.hidden_at or client.is_blocked:
        return {}
    candidates = [task for task in list(tasks)[:50] if task.client_id == client.pk
        and task.kind == 'manager_task' and task.reason == 'revision_case:product_configuration_review'
        and task.status not in {'completed', 'cancelled', 'sent', 'ambiguous'}
        and task.manager_approval_status == 'pending' and isinstance(task.manager_context, dict)
        and task.manager_context.get('case_kind') == 'product_configuration_review'][:8]
    if not candidates:
        return {}
    settings = InstagramBotSettings.objects.only('pk', 'ig_user_id', 'page_id', 'reply_permission_epoch').order_by('pk').first()
    namespace = ingress_provider_namespace(settings) if settings else ''
    if not namespace:
        return {}
    floor = conversation_route_reset_floor(client.pk) if reset_floor is None else reset_floor
    ids = {task.manager_context.get('latest_revision_id') for task in candidates
        if type(task.manager_context.get('latest_revision_id')) is int}
    owners = {row.pk: row for row in IgCustomerTurnRevision.objects.filter(pk__in=ids,
        client_id=client.pk, permission_epoch=client.reply_permission_epoch)}
    source_rows = list(IgTurnRevisionSource.objects.filter(revision_id__in=owners)
        .select_related('message__client').order_by('revision_id', 'ordinal', 'id')[:257])
    if len(source_rows) > 256:
        return {}
    grouped = {}
    for row in source_rows:
        grouped.setdefault(row.revision_id, []).append(row)
    receipts = {owner.pk: (owner.action_receipts.get('manager_handoff') or {})
        if isinstance(owner.action_receipts, dict) else {} for owner in owners.values()}
    notification_ids = {value.get('notification_id') for value in receipts.values()
        if isinstance(value, dict) and type(value.get('notification_id')) is int}
    notifications = IgBotNotification.objects.filter(pk__in=notification_ids, client_id=client.pk).in_bulk()
    journals = list(IgConversationRouteDecision.objects.filter(client_id=client.pk,
        watermark_message_id__in=[row.message_id for row in source_rows]).order_by('-sequence')[:257])
    if len(journals) > 256:
        return {}
    current_cart = capture_current_selection_lines(client.pk)
    if current_cart.get('status') != 'captured':
        return {}
    output = {}
    for task in candidates:
        owner = owners.get(task.manager_context.get('latest_revision_id'))
        if owner is None:
            continue
        owner.client = client
        try:
            snapshots = owner.bundle_snapshot.get('sources') or []
            source_ids = [row['message_id'] for row in snapshots]
            if (not 1 <= len(source_ids) <= 32 or len(set(source_ids)) != len(source_ids)
                or any(type(pk) is not int or pk < floor for pk in source_ids)
                or any(row.get('role') != 'user' or row.get('source_namespace') != namespace for row in snapshots)
                or owner.snapshot_digest != _digest(owner.bundle_snapshot)
                or owner.erasure_started_at_snapshot != client.privacy_erasure_started_at
                or not revision_sources_current(owner, source_rows=grouped.get(owner.pk, []))):
                continue
            proposal = owner.generation_proposal or {}
            holding = (owner.action_receipts or {}).get('technical_holding') or {}
            if proposal:
                if not owner.generation_proposal_digest or owner.generation_proposal_digest != _digest(proposal):
                    continue
                execution = proposal.get('execution_binding') or {}
                cart = proposal.get('source_cart_capture') or {}
            else:
                execution = holding
                cart = (holding.get('proof') or {}).get('source_cart_capture') or {}
            if (execution.get('settings_id') != settings.pk
                or execution.get('settings_permission_epoch') != settings.reply_permission_epoch):
                continue
            captured_scope = cart.get('scope') or {}
            if cart and (cart.get('status') != 'captured'
                or cart.get('capture_digest') != _digest({key: value for key, value in cart.items() if key != 'capture_digest'})
                or captured_scope.get('client_id') != client.pk or captured_scope.get('reset_floor') != floor
                or captured_scope.get('source_namespace') != namespace
                or cart_identity(cart) != cart_identity(current_cart)):
                continue
            journal = next((row for row in journals if row.watermark_message_id in source_ids), None)
            routed_episode = _accepted_scope(journal, source_ids).get('commercial_episode_id')
            episode = captured_scope.get('episode_id') or routed_episode
            if (routed_episode and captured_scope.get('episode_id') and routed_episode != captured_scope['episode_id']
                or episode != client.current_commercial_episode_id):
                continue
            receipt = receipts[owner.pk]
            notification = notifications.get(receipt.get('notification_id'))
            if notification is None or product_review_receipt_reason(owner, task=task, notification=notification):
                continue
            inquiry = product_inquiries_for_revision(owner)
            witnesses = [row for row in inquiry.get('inquiries') or []
                if row.get('source_message_id') in source_ids and row.get('axis') in {'color', 'size'}]
            question = ' · '.join(dict.fromkeys(row['question'] for row in witnesses))[:500]
            if not question:
                continue
            output[task.pk] = {'state': 'pending', 'actionable': True, 'type': 'product_configuration_review',
                'client_id': client.pk, 'task_id': task.pk,
                'source_message_ids': list(dict.fromkeys(row['source_message_id'] for row in witnesses)), 'question': question}
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return output
