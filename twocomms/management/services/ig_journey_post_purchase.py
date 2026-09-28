"""Order-scoped continuations, not completed events or permission to send."""
from copy import deepcopy
from management.services.ig_journey_catalogue import journey_catalogue

KEYS = ('post_sale_case', 'channel_consent', 'channel_grant_checked',
        'post_purchase_contact_offer', 'ugc_assessment', 'reward_entitlement',
        'reward_delivery', 'reward_use', 'repeat_interest', 'new_purchase_interest')
NOTES = {
    'post_sale_case': ('Сервіс доступний', 'Питання доставки, розміру, обмін або проблема — окремий сервісний випадок. Він не залежить від маркетингової згоди.'),
    'channel_consent': ('Окрема згода', 'Запрошення, відповідь клієнта та чинний дозвіл перевіряються окремо.'),
    'channel_grant_checked': ('Перевірити дозвіл', 'Перевірка теми, каналу та строку дозволу перед кожним повідомленням.'),
    'post_purchase_contact_offer': ('Потрібна згода', 'Отримання підтверджене. Для пропозиції потрібні чинний дозвіл і підтвердження відправлення.'),
    'ugc_assessment': ('Чекаємо матеріал', 'Відмітка, фото або відгук можуть надійти без запрошення. Потрібні перевірка матеріалу та його зв’язку із замовленням.'),
    'reward_entitlement': ('Після перевірки UGC', 'Нагорода лише після перевірки умов і підтвердження права на неї.'),
    'reward_delivery': ('Після призначення', 'Показуємо видачу лише за підтвердженням доставки нагороди.'),
    'reward_use': ('Після видачі', 'Використання підтверджується окремою операцією; видача не означає використання.'),
    'repeat_interest': ('За бажанням клієнта', 'Повторний інтерес — окремий сигнал клієнта, незалежно від нагороди.'),
    'new_purchase_interest': ('Окрема покупка', 'Новий запит створює окремий цикл; попередня доставка не завершує нове замовлення.'),
}


def append_post_purchase_context(graph, *, is_history=False):
    result = deepcopy(graph)
    if is_history:
        return result
    catalogue = journey_catalogue()
    definitions = {d['key']: d for d in catalogue['definitions']}
    parents = [n for n in result['nodes'] if n.get('fulfillment_progress', {}).get('evidence_refs')
               and n.get('semantic_key') in {'client_order_context', 'fulfillment'}]
    for parent in parents:
        progress = parent['fulfillment_progress']
        if progress.get('cancelled') or (progress.get('step', 0) < 2 and not progress.get('completion_unverified')):
            continue
        received = progress['step'] == 4
        order_refs = [r for r in progress['evidence_refs'] if r.get('kind') == 'order']
        if len(order_refs) != 1:
            continue
        order_id = order_refs[0]['id']
        anchors = {'fulfillment': parent['id']}
        for key in KEYS:
            # Reuse only facts within this order's episode or explicit order scope.
            matches = [n for n in result['nodes'] if (n.get('structural_key') or n.get('semantic_key')) == key
                       and ((n.get('contextual_binding') or {}).get('order_id') == order_id
                            or (parent.get('episode_id') is not None and n.get('episode_id') == parent['episode_id']))]
            if key == 'channel_consent' and not matches:
                matches = [n for n in result['nodes'] if n.get('semantic_key') == 'client_order_contact'
                           and n.get('contextual_binding', {}).get('order_id') == order_id]
            if len(matches) > 1:
                continue  # Ambiguous facts must not be collapsed into one outcome.
            if matches:
                node = matches[0]
                node['structural_key'] = key
            else:
                definition = definitions[key]
                node = {'id': f'post-purchase:{order_id}:{key}', 'semantic_key': key,
                        'structural_key': key, 'label': definition['label'], 'current': False,
                        'presentation_kind': 'possible', 'state': None, 'facts': [], 'evidence_refs': [],
                        'producer': 'post_purchase_context', 'scope': parent.get('scope'),
                        'episode_id': parent.get('episode_id'), 'contextual_binding': {'order_id': order_id},
                        **{k: definition[k] for k in ('implementation_status', 'implementation_note') if k in definition}}
                result['nodes'].append(node)
            label, note = NOTES[key]
            readiness = 'available' if key == 'post_sale_case' or received and key in {'ugc_assessment', 'repeat_interest'} else 'conditional'
            if not received and key not in {'post_sale_case', 'channel_consent', 'channel_grant_checked'}:
                readiness, label = 'waiting', 'Після отримання'
                note = 'Отримання ще не підтверджено. ' + note.replace('Отримання підтверджене. ', '')
            consent = deepcopy(node.get('consent_progress') or result.get('marketing_consent', {}))
            if key in {'channel_consent', 'channel_grant_checked'} and consent:
                consent['delivery'] = {'status': 'received' if received else 'waiting', 'evidence_refs': order_refs if received else []}
                node['consent_progress'] = consent
            if key in {'channel_consent', 'channel_grant_checked', 'post_purchase_contact_offer'} and consent.get('permission', {}).get('status') == 'blocked':
                readiness, label, note = 'blocked', 'Контакт заборонено', 'Заборона повідомлень зберігається після доставки. Сервіс за зверненням клієнта — окрема гілка.'
            node['post_purchase'] = {'order_id': order_id, 'parent_id': parent['id'], 'received': received,
                                     'readiness': readiness, 'label': label, 'note': note, 'evidence_refs': order_refs}
            anchors[key] = node['id']
        parent['post_purchase_node_ids'] = [anchors[k] for k in KEYS if k in anchors]
        transitions = [t for t in catalogue['transitions'] if t['source_key'] in anchors and t['target_key'] in anchors]
        # Incoming UGC and repeat interest are independent of outbound marketing.
        transitions.append({'id': 'incoming-ugc', 'source_key': 'fulfillment', 'target_key': 'ugc_assessment', 'condition_label': 'Якщо клієнт надіслав матеріал'})
        for transition in transitions:
            source, target = anchors[transition['source_key']], anchors[transition['target_key']]
            if any(e['from_node_id'] == source and e['to_node_id'] == target for e in result['edges']):
                continue
            result['edges'].append({'id': f"post-purchase:{order_id}:{transition['id']}",
                'from_node_id': source, 'to_node_id': target, 'relation': 'route',
                'post_purchase_order_id': order_id, 'evidence_refs': [],
                'condition_label': transition.get('condition_label') or NOTES[transition['target_key']][0],
                'summary': 'Можливе продовження цього замовлення; не підтвердження виконаної дії.'})
    return result
