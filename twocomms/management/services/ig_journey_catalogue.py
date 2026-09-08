"""Static possible paths from the canonical registry, never client history."""
from functools import lru_cache
from management.services.ig_funnel_nodes import DEFINITION_VERSION, semantic_definitions, structural_transitions

PLANNED_AUTOMATIONS = {
    "stock_wait": "Планується зв’язок очікування з точним варіантом товару та змінами наявності. Дозвіл на повідомлення перевірятиметься окремо.",
    "restock_consent": "Планується окреме підтвердження дозволу на сповіщення про наявність. Обіцянка менеджера ще не оформлює підписку.",
    "channel_consent": "Планується підтвердження згоди для конкретної теми та каналу. Обговорення або демонстраційна кнопка не надають дозволу на відправлення.",
    "post_purchase_contact_offer": "Планується пропозиція після покупки з перевіркою отримання замовлення та дозволу на повідомлення.",
    "custom_brief": "Планується збереження версій брифа та перевірка його повноти. Обговорений у чаті бриф уже може відображатися на шляху.",
    "mockup_current_acceptance": "Планується прив’язка згоди до конкретної версії макета перед виробництвом. Обговорення зображення не замінює цю перевірку.",
    "prize_decision": "Планується підтвердження командою умов призу й суми до сплати. Повне призове покриття дозволятиме виконання без додаткової оплати.",
}


@lru_cache(maxsize=1)
def journey_catalogue():
    return {
        "version": DEFINITION_VERSION,
        "definitions": [{"key": item.key, "label": item.ui_label,
            "route_keys": list(item.route_keys), "semantic_kind": item.semantic_kind,
            **({"implementation_status": "planned", "implementation_note": PLANNED_AUTOMATIONS[item.key]}
               if item.key in PLANNED_AUTOMATIONS else {})}
            for item in semantic_definitions()],
        "transitions": [{"id": f"structural:{i}:{item.source_key}:{item.target_key}",
            "source_key": item.source_key, "target_key": item.target_key,
            "outcome": item.outcome} for i, item in enumerate(structural_transitions())],
        "history": False, "actions": False,
    }
