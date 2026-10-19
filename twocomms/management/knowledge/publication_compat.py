"""Finite reviewed replacements for the six captured legacy instruction bodies.

Pure values only: no ORM, provider or publication I/O. The caller must keep
all unrelated draft changes out of the publication and use the existing
save/preview/publish CAS flow. Hashes identify the 20-row live publication
captured read-only on 2026-10-19; no fuzzy matching of administrator edits.
"""
from copy import deepcopy
from hashlib import sha256
from collections.abc import Mapping

VERSION = "2026-10-18.owner-legacy-compatibility.1"
SOURCE = "owner:2026-10-18:Q01,Q02,Q03,Q05,Q07,Q11,Q12,Q19"

LEGACY_BODY_SHA256 = {'IG Objection · Є дешевше': '5537d7139e886bae86f4179cc94a4f485954969931729ca0f39ba55df4bac2b7',
 'IG Objection · Дорого': 'a73235fb2d2504d95448334784b6b9cda8db5bffcf0fb44c4b2ab991cf3369de',
 'IG Objection · Порівняння брендів': 'e8905bf54a9db207746dbd9f4da7a3c8bdce5987a6869d1e40033417266fda6c',
 'IG Objection · Якість принта': '0e4d6f53a018ab25f5706e0242dac818502aab692c4e09e3e2b6e9c927f58fb0',
 'Prepayment Objection': 'd93280367ba0c7eadb69f4c5ba1e17708abfdda1cece4a9ae76b5b62df35cadd',
 'Size And Fit': '684c99d7d46d4d693788ef16abd0544a0ff36df8c7795835b4062c52ee0c8151'}

REVIEWED_BODIES = {'IG Objection · Є дешевше': 'Не сперечайся з ціною конкурента й не приписуй йому гіршу якість. Порівняй '
                             'лише перевірені характеристики конкретних речей. Для нашої підтвердженої '
                             'поточної основи можна використовувати застосовні captured reviewed '
                             'approved_public_facts про склад, щільність, крій, догляд і друк разом із '
                             'карткою товару. Збережи scope основи, приблизність щільності та авторство й '
                             'умови оцінки ресурсу; не виводь із них незалежний рейтинг, універсальну '
                             'перевагу чи властивості невідомої речі. Поточну ціну й доступну знижку бере '
                             'checkout. Додай JSON control '
                             '{"kind":"objhandle","value":"cheaper_elsewhere:value_breakdown"} (ніколи не '
                             'додавай його до reply_text).',
 'IG Objection · Дорого': 'Визнач актуальний ціновий бар’єр без тиску та поясни поточну розраховану ціну '
                          'вибраного товару. Один доречний аргумент можна взяти з підтвердженої картки або '
                          'захоплених reviewed approved_public_facts для точно відповідної поточної основи: '
                          'current_tshirt_bases, current_hoodie_bases, current_care чи '
                          'current_print_quality. Не поширюй характеристику на чужу або невідому основу; '
                          'оцінку ресурсу засновником передавай лише з авторством і умовами догляду. Не '
                          'заявляй непідтвердженої переваги бренду, не знецінюй конкурентів і не призначай '
                          'автоматичну знижку; права й остаточні суми визначає сервер. Після цього додай '
                          'JSON control {"kind":"objhandle","value":"price:value_breakdown"} (ніколи не '
                          'додавай його до reply_text).',
 'IG Objection · Порівняння брендів': 'Не знецінюй інший бренд і не вигадуй порівняльний тест. Дай один '
                                      'доречний перевірюваний факт про вибраний товар або застосовну поточну '
                                      'основу з captured reviewed approved_public_facts; для походження чи '
                                      'історії бренду потрібне окреме підтверджене джерело. Підтверджений '
                                      'склад, приблизну щільність, крій, догляд і оцінку ресурсу друку можна '
                                      'пояснювати у їхньому scope; не перенось їх на весь каталог чи '
                                      'конкурента. Понад 100 прань передавай лише як оцінку засновника при '
                                      'рекомендованому догляді, не лабораторний доказ. Походження тканини не '
                                      'доводить місце пошиття. Для доказів додай JSON control '
                                      '{"kind":"objhandle","value":"compare_brand:social_proof"} (ніколи не '
                                      'додавай його до reply_text), для розкладу цінності — JSON control '
                                      '{"kind":"objhandle","value":"compare_brand:value_breakdown"} (ніколи '
                                      'не додавай його до reply_text).',
 'IG Objection · Якість принта': 'Для підтверджених поточних футболок і худі використовуй captured reviewed '
                                 'approved_public_facts current_print_quality та current_care разом із '
                                 'ярликом конкретної речі: понад 100 прань — оцінка засновника за '
                                 'рекомендованого догляду при 30°C, без агресивної хімії. Це не лабораторний '
                                 'тест і не обіцянка вічності, незмінності чи автоматичної заміни через '
                                 'певний строк. Не поширюй ці факти на іншу основу або власний одяг клієнта '
                                 'без підтвердження. Можна запропонувати вже наявний approved крупний план '
                                 'чи відео; для нового матеріалу передай конкретний товар, розмір і потрібну '
                                 'деталь команді, не вдаючи, що файл уже готовий. При проблемі команда '
                                 'розгляне сервісне звернення. Додай JSON control '
                                 '{"kind":"objhandle","value":"print_quality:print_quality"} (ніколи не '
                                 'додавай його до reply_text).',
 'Prepayment Objection': 'Повна оплата — основний шлях. Після корисної консультації щодо розміру, подарунка '
                         'або стійкого сумніву щодо якості чи довіри можна предметно обговорити 200 грн '
                         'передоплати з післяплатою, лише якщо поточний checkout і source-bound погодження '
                         'цього замовлення це дозволяють. Сам текст інструкції не відкриває спосіб оплати. '
                         'При викупі 200 грн зараховуються у вартість товару; при звичайній відмові після '
                         'огляду утримуються всі 200 грн, а не лише фактичні витрати перевізника. Доставка й '
                         'комісія післяплати — окремо за чинними умовами перевізника; не вигадуй відсоток '
                         'комісії та не обіцяй безкоштовну доставку при післяплаті. Підтверджений брак або '
                         'помилка магазину — окремий сервісний випадок. Custom залишається з повною оплатою. '
                         'Не перенось платіж чи згоду з іншого замовлення та не вигадуй розподіл кількох '
                         'тестових посилок.',
 'Size And Fit': 'Збережи вибрані товар і принт. Для підтвердженої футболки regular/oversize поясни '
                 'відмінність і постав одне питання про крій лише якщо його ще не обрано. Поточне худі має '
                 'єдиний крій — злегка oversize, унісекс; не проси обрати classic/oversize для нього. '
                 'Порівнюй мірки зручної речі та бажану свободу з точною сіткою обраної основи й версії. '
                 'Покажи лише наявне підтверджене зображення відповідної сітки. Нова числова сітка худі '
                 'очікується: не вигадуй плечі, рукав чи допуски та не підставляй сітку футболки. Не '
                 'гарантуй посадку й не обирай розмір замість клієнта.'}

LEGACY_COMPATIBILITY_TITLES = frozenset(LEGACY_BODY_SHA256)
_VALUE_FIELDS = ("title", "body", "active", "priority", "locale", "tags", "triggers",
                 "programme_metadata", "allowed_actions", "trust_scope")


def reviewed_legacy_instruction_values(item):
    """Return detached save_instruction_draft values, or None if not reviewed.

    Already-upgraded exact bodies return unchanged values for idempotent
    planning. A known title with any other body returns None: the publisher
    must surface that row for separate review, never infer seed ownership.
    Existing bound reviewed_source provenance is not invalidated by an upgrade.
    """
    if not isinstance(item, Mapping) or not set(_VALUE_FIELDS).issubset(item):
        return None
    title, body = item["title"], item["body"]
    if title not in LEGACY_BODY_SHA256 or not isinstance(body, str):
        return None
    reviewed = REVIEWED_BODIES[title]
    already_reviewed = body == reviewed
    if not already_reviewed:
        if sha256(body.encode("utf-8")).hexdigest() != LEGACY_BODY_SHA256[title]:
            return None
        if item.get("reviewed_source") or item["trust_scope"] != "public_policy":
            return None
    values = {name: deepcopy(item[name]) for name in _VALUE_FIELDS}
    if "reviewed_source" in item:
        values["reviewed_source"] = deepcopy(item["reviewed_source"])
    values["body"] = reviewed
    return values


def legacy_instruction_compatibility_plan(items):
    """Return body-only proposals. None values for known titles require review.

    IDs are retained only as the target of the existing draft API. No row,
    routing tag, trigger, priority, active bit or control capability is created.
    """
    proposals = []
    for item in items:
        values = reviewed_legacy_instruction_values(item)
        if values is None or values["body"] == item["body"]:
            continue
        proposals.append({
            "version": VERSION,
            "instruction_id": item["source_id"],
            "before_body_digest": sha256(item["body"].encode("utf-8")).hexdigest(),
            "after_body_digest": sha256(values["body"].encode("utf-8")).hexdigest(),
            "values": values,
        })
    return proposals
