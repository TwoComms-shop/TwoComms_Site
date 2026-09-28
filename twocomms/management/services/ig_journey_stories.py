"""Read-only story interactions, independent of purchase and reward eligibility.

Never fetch expiring media or infer a sent reply from neighbouring chat text.
Only matching per-part inspection and exact provider reply identities count.
"""
from copy import deepcopy
from django.db.models import Q
from django.db.models.functions import Substr
from management.models import IgClient, InstagramBotMessage, IgRevisionDeliveryEffect
from management.services.ig_conversation_routes import conversation_route_reset_floor

MESSAGE_LIMIT = 80
ITEM_LIMIT = 24
KINDS = {'story', 'story_mention', 'share', 'ig_post', 'ig_reel', 'reel'}
THEMES = {'product': 'Товар / одяг', 'custom_reference': 'Ідея власного принта',
          'selfie': 'Фото людей', 'certificate': 'Сертифікат', 'receipt': 'Квитанція',
          'payment_screenshot': 'Оплата', 'document': 'Документ',
          'other': 'Інший сюжет', 'unknown': 'Тему не визначено'}


def _text(value, limit=600):
    return value[:limit] if isinstance(value, str) else ''


def append_stories(graph, *, client_id, is_history):
    result = deepcopy(graph)
    if is_history or not IgClient.objects.filter(pk=client_id, privacy_erasure_started_at__isnull=True).exists():
        return result
    floor = conversation_route_reset_floor(client_id)
    rows = list(InstagramBotMessage.objects.filter(client_id=client_id, role='user', pk__gte=floor,
        private_media_state__in=['', 'active']).exclude(attachment_media=[])
        .order_by('-pk').annotate(excerpt=Substr('text', 1, 600))
        .values('id', 'mid', 'provider_namespace', 'created_at', 'provider_created_at', 'excerpt', 'attachment_media')[:MESSAGE_LIMIT + 1])
    candidates = []
    for row in rows[:MESSAGE_LIMIT]:
        media = row['attachment_media'] if isinstance(row['attachment_media'], list) else []
        seen = set()
        for index, part in enumerate(media[:16]):
            if not isinstance(part, dict):
                continue
            kind = part.get('media_type')
            if not isinstance(kind, str) or kind not in KINDS:
                continue
            identity = part.get('source_part_id') or part.get('provider_object_key') or str(index)
            if not isinstance(identity, str) or identity in seen:
                continue
            seen.add(identity)
            candidates.append((row, part, index))
    if not candidates:
        return result
    truncated = len(rows) > MESSAGE_LIMIT or len(candidates) > ITEM_LIMIT
    candidates = candidates[:ITEM_LIMIT]
    ids = list({row['id'] for row, _, _ in candidates})
    # A normal reply may target the revision's anchor message. Only direct source
    # ownership is projected here; ambiguous/burst associations remain unknown.
    effects = list(IgRevisionDeliveryEffect.objects.filter(source_message_id__in=ids,
        source_message__client_id=client_id, revision__client_id=client_id,
        state='sent', purpose='normal_reply').exclude(provider_message_id='')
        .values('source_message_id', 'provider_message_id', 'provider_namespace').order_by('-pk')[:96])
    mids = [r['mid'] for r, _, _ in candidates if r['mid']]
    replies = list(InstagramBotMessage.objects.filter(client_id=client_id, pk__gte=floor,
        role__in=['model', 'manager'], status='done').filter(
        Q(reply_to_provider_message_id__in=mids) | Q(provider_message_id__in=[e['provider_message_id'] for e in effects]))
        .exclude(provider_message_id='').annotate(excerpt=Substr('text', 1, 800))
        .values('id', 'excerpt', 'role', 'provider_namespace', 'provider_message_id', 'reply_to_provider_message_id')
        .order_by('pk')[:96])
    items = []
    for row, part, index in candidates:
        inspection = part.get('inspection') if isinstance(part.get('inspection'), dict) else {}
        owned = part.get('status') == 'owned' or part.get('capture_state') == 'owned'
        inspected = bool(owned and inspection.get('state') == 'inspected'
            and part.get('source_part_id') and inspection.get('source_part_id') == part['source_part_id']
            and part.get('content_hash') and inspection.get('content_hash') == part['content_hash'])
        outcome = _text(inspection.get('outcome'),32) if inspected else 'uninspected'
        native = part.get('provider_native_mention') is True and part.get('target_username') == 'twocomms'
        kind = 'reply' if part.get('interaction_kind') == 'story_reply' else 'mention' if native else 'share' if part.get('media_type') in {'share', 'ig_post', 'ig_reel', 'reel'} else 'story'
        matches = [r for r in replies if r['provider_namespace'] == row['provider_namespace'] and (
            bool(row['mid'] and r['reply_to_provider_message_id'] == row['mid']) or any(
                e['source_message_id'] == row['id'] and e['provider_namespace'] == r['provider_namespace']
                and e['provider_message_id'] == r['provider_message_id'] for e in effects))]
        items.append({'id': f"story:{row['id']}:{index}", 'kind': kind,
            'label': {'reply': 'Відповідь на сторис', 'mention': 'Відмітка TwoComms', 'share': 'Поширений допис / репост', 'story': 'Сторис'}[kind],
            'received_at': (row['provider_created_at'] or row['created_at']).isoformat(),
            'native_mention': native, 'media_available': owned, 'inspected': inspected,
            'outcome': outcome, 'theme': THEMES.get(_text(inspection.get('type_code')), 'Тему не визначено') if inspected else '',
            'customer_text': row['excerpt'],
            'replies': [{'text': r['excerpt'], 'actor': r['role'], 'evidence_refs': [{'kind': 'message', 'id': r['id']}]} for r in matches],
            'evidence_refs': [{'kind': 'message', 'id': row['id']}]})
    # Honour erasure/reset beginning during the bounded read as well.
    if not IgClient.objects.filter(pk=client_id, privacy_erasure_started_at__isnull=True).exists() or conversation_route_reset_floor(client_id) != floor or InstagramBotMessage.objects.filter(pk__in=ids, client_id=client_id, private_media_state__in=['', 'active']).count() != len(ids):
        return result
    node_id = f'stories:{client_id}'
    refs = [ref for item in items for ref in item['evidence_refs']]
    node = {'id': node_id, 'semantic_key': 'story_interactions', 'producer': 'story_context',
        'scope': 'client', 'presentation_kind': 'client_context', 'state': 'partial',
        'label': 'Сторис та відмітки', 'short_label': 'Сторис', 'facts': [], 'evidence_refs': refs,
        'story_interactions': {'schema': 'journey-stories.v1', 'items': items, 'truncated': truncated,
            'count': len(items), 'reply_count': sum(bool(i['replies']) for i in items)},
        'summary': 'Взаємодія з магазином незалежно від каналу покупки. Відмітка не підтверджує покупку, право на нагороду або дозвіл на маркетинг.'}
    result['nodes'] = [n for n in result['nodes'] if n['id'] != node_id] + [node]
    result['edges'] = [e for e in result.get('edges', []) if e['id'] != f'story-context:{client_id}']
    origin = next((n for n in result['nodes'] if n.get('semantic_key') == 'inbound'), None)
    if origin:
        result['edges'].append({'id': f'story-context:{client_id}', 'from_node_id': origin['id'], 'to_node_id': node_id,
            'relation': 'story_context', 'condition_label': 'Сторис / взаємодія з магазином', 'evidence_refs': refs})
    return result
