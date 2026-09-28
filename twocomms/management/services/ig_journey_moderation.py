"""Current moderation state and source-backed warning, without sending actions."""
from copy import deepcopy
import re
from django.db.models.functions import Substr
from management.models import IgClient, InstagramBotMessage
from management.services.ig_conversation_routes import conversation_route_reset_floor

_WARNING = re.compile(r'(?:попередж|предупреж|warn|заблоку|заблокир|block)', re.I)
_SPAM = re.compile(r'\b(?:спам|spam)|образ|оскорб|abuse', re.I)


def append_moderation(graph, *, client_id, is_history):
    result = deepcopy(graph)
    if is_history:
        return result
    client = IgClient.objects.filter(pk=client_id, privacy_erasure_started_at__isnull=True).values(
        'stage', 'spam_strikes', 'bot_paused', 'paused_reason', 'is_blocked').first()
    if not client:
        return result
    spam = client['stage'] == 'spam' or client['spam_strikes'] > 0 or client['paused_reason'] == 'spam'
    if not spam:
        return result
    refs = [{'kind': 'client', 'id': client_id}]
    floor = conversation_route_reset_floor(client_id)
    messages = InstagramBotMessage.objects.filter(client_id=client_id, pk__gte=floor,
        role__in=['model', 'manager'], status='done').exclude(provider_message_id='').order_by('-pk').annotate(excerpt=Substr('text', 1, 1500)).values('id', 'excerpt')[:100]
    warning = next((m for m in messages if _WARNING.search(m['excerpt']) and _SPAM.search(m['excerpt'])), None)
    stopped = bool(client['is_blocked'] or client['bot_paused'])
    progress = {'schema': 'journey-moderation.v1', 'marked': {'status': 'recorded', 'evidence_refs': refs},
        'warning': {'status': 'sent' if warning else 'unknown', 'evidence_refs': [{'kind': 'message', 'id': warning['id']}] if warning else []},
        'processing': {'status': 'stopped' if stopped else 'active', 'evidence_refs': refs},
        'strikes': client['spam_strikes'],
        'mark_source': 'classifier' if client['spam_strikes'] else 'unspecified',
        'note': 'Поточний стан картки. Страйк не доводить, що попередження надіслано. Пауза/блокування не підтверджує видалення попередніх даних.'}
    nodes = [n for n in result['nodes'] if n.get('semantic_key') == 'spam_confirmed']
    if not nodes:
        node = {'id': f'guide:moderation:{client_id}', 'semantic_key': 'spam_confirmed', 'producer': 'moderation_context',
                'scope': 'client', 'presentation_kind': 'client_context', 'state': 'partial', 'label': 'Спам / обмеження',
                'short_label': 'Спам', 'facts': [], 'evidence_refs': refs}
        result['nodes'].append(node); nodes = [node]
        origin = next((n for n in result['nodes'] if n.get('semantic_key') == 'inbound'), None)
        if origin:
            result['edges'].append({'id': f'moderation-context:{client_id}', 'from_node_id': origin['id'], 'to_node_id': node['id'], 'relation': 'moderation_context', 'evidence_refs': refs, 'condition_label': 'Стан модерації картки'})
    for node in nodes:
        node['moderation_progress'] = progress
    return result
