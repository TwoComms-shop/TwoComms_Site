"""Public storefront projections of the reviewed B02.8 policy facts.

The provider receives source/capability directives alongside the facts.  Site
and checkout consumers must use only ``public_text`` from that same typed,
versioned publication.
"""
from __future__ import annotations

from typing import Any

from management.services.approved_public_facts import approved_public_fact_manifest
from storefront.support_content import DELIVERY_FAQ_ITEMS
from django.utils.translation import override


POLICY_KEYS = ("dispatch", "service_boundary")
_SERVICE_QUESTION = {
    "uk": "Що підготувати для сервісного звернення?",
    "ru": "Что подготовить для сервисного обращения?",
    "en": "What should I prepare for a service request?",
}
_LEGACY_DISPATCH_META = {
    "uk": "1-5 днів по Україні",
    "ru": "1-5 дней по Украине",
    "en": "1-5 days within Ukraine",
}
_PAYMENT_QUESTION = {
    "uk": "Чи можна оплатити товар при отриманні?",
    "ru": "Можно ли оплатить при получении?",
    "en": "Can I pay on delivery?",
}
_DELAY_QUESTION = {
    "uk": "Що робити, якщо посилка затрималась?",
    "ru": "Что делать, если посылка задерживается?",
    "en": "What should I do if my shipment is delayed?",
}


class PolicyCopyReadinessError(RuntimeError):
    """A localized support page no longer has the reviewed policy slot."""


_SLOT_LABELS = {
    "uk": {
        "delivery_section": "Базовий сценарій доставки",
        "delivery_card": "Строки",
        "delivery_question": "Як довго триває доставка по Україні?",
        "returns_section": "Для яких замовлень діє повернення та обмін",
        "returns_service_question": "Які умови повернення товару?",
        "returns_custom_question": "",
        "help_section": "Кастомний друк і сервісні звернення",
        "help_service_question": "Як подати сервісне звернення після отримання замовлення?",
        "faq_delivery_question": "Скільки триває доставка по Україні?",
        "faq_service_question": "Що робити, якщо розмір не підійшов?",
        "dispatch_hero_meta": "відправлення 1–3 дні",
    },
    "ru": {
        "delivery_section": "Базовый сценарий доставки",
        "delivery_card": "Сроки",
        "delivery_question": "Сколько идёт доставка по Украине?",
        "returns_section": "Для каких заказов действует возврат и обмен",
        "returns_service_question": "Какие товары можно вернуть или обменять?",
        "returns_custom_question": "Можно ли вернуть кастомную одежду?",
        "help_section": "Кастомная печать и сервисные обращения",
        "help_service_question": (
            "Як подати сервісне звернення після отримання замовлення?",
            "Как подать сервисное обращение после получения заказа?",
        ),
        "faq_delivery_question": "Сколько идёт доставка по Украине?",
        "faq_service_question": "Какие условия возврата и обмена?",
        "dispatch_hero_meta": "отправка 1–3 дня",
    },
    "en": {
        "delivery_section": "Standard delivery scenario",
        "delivery_card": "Timelines",
        "delivery_question": "How long does delivery within Ukraine take?",
        "returns_section": "Which orders are eligible",
        "returns_service_question": "Which items can be returned or exchanged?",
        "returns_custom_question": "Can custom apparel be returned?",
        "help_section": "Custom print and service requests",
        "help_service_question": (
            "Як подати сервісне звернення після отримання замовлення?",
            "How do I submit a service request after receiving an order?",
        ),
        "faq_delivery_question": "How long is delivery within Ukraine?",
        "faq_service_question": "What are the return and exchange terms?",
        "dispatch_hero_meta": "dispatch in 1–3 days",
    },
}


def _labels(language: str) -> dict[str, str | tuple[str, ...]]:
    try:
        return _SLOT_LABELS[language]
    except KeyError as exc:
        raise PolicyCopyReadinessError(f"unsupported public policy language: {language}") from exc


def _one(items: list[dict[str, Any]], field: str, expected: str | tuple[str, ...], slot: str) -> dict[str, Any]:
    labels = (expected,) if isinstance(expected, str) else expected
    matches = [item for item in items if str(item.get(field, "")) in labels]
    if len(matches) != 1:
        raise PolicyCopyReadinessError(f"required policy slot missing or ambiguous: {slot}")
    return matches[0]


def _remove_one(items: list[Any], expected: tuple[str, ...], slot: str) -> list[Any]:
    kept = [item for item in items if str(item) not in expected]
    if len(kept) != len(items) - 1:
        raise PolicyCopyReadinessError(f"required policy slot missing or ambiguous: {slot}")
    return kept


def approved_policy_copy(language: str, *, keys: tuple[str, ...] = POLICY_KEYS) -> dict[str, Any]:
    """Return clean text plus a non-display publication identity for a surface."""
    manifest = approved_public_fact_manifest(language)
    facts = {fact.key: fact.public_text for fact in manifest["facts"]}
    missing = set(keys).difference(facts)
    if missing:
        raise KeyError(f"unknown approved policy copy keys: {sorted(missing)!r}")
    return {
        "text": {key: facts[key] for key in keys},
        "metadata": {
            "version": manifest["version"],
            "content_hash": manifest["content_hash"],
            "language": language,
        },
    }


def apply_support_policy_copy(page: dict[str, Any], page_key: str, language: str) -> dict[str, Any]:
    """Replace the known public policy slots after locale overrides run.

    These paths intentionally cover only the existing delivery/returns/help/
    FAQ duplicates.  They do not alter custom-print content or unrelated
    support material.
    """
    labels = _labels(language)
    policy = approved_policy_copy(language, keys=(*POLICY_KEYS, "service_request", "delivery_delay", "payment_boundary"))["text"]
    dispatch = policy["dispatch"]
    service = policy["service_boundary"]

    if page_key == "delivery":
        # Delivery page: ordinary preparation/dispatch, never carrier transit.
        page["hero_meta"] = _remove_one(
            list(page.get("hero_meta", ())),
            (labels["dispatch_hero_meta"], _LEGACY_DISPATCH_META[language]),
            "delivery.hero_meta.dispatch"
        )
        section = _one(page.get("sections", []), "title", labels["delivery_section"], "delivery.section")
        card = _one(section.get("cards", []), "title", labels["delivery_card"], "delivery.timelines_card")
        card["text"] = dispatch
        dispatch_faq = _one(page.get("faq_items", []), "question", labels["delivery_question"], "delivery.faq")
        dispatch_faq["answer"] = dispatch
        page["meta_title"] = page["page_title"]
        # Keep only supported delivery/payment answers; route-specific options
        # remain in the surrounding sections and current checkout.
        # Preserve the checkout-owned threshold and its full-payment condition.
        # Localized legacy overrides omitted this FAQ entirely; taking the lazy
        # source here keeps visible HTML and JSON-LD on the same current rule.
        with override("uk"):
            shipping_cost_faq = next(
                item for item in DELIVERY_FAQ_ITEMS
                if str(item["question"]) == "Скільки коштує доставка?"
            )
        with override(language):
            shipping_cost_faq = {
                "question": str(shipping_cost_faq["question"]),
                "answer": str(shipping_cost_faq["answer"]),
            }
        page["faq_items"] = [dispatch_faq, shipping_cost_faq, {
            "question": _PAYMENT_QUESTION[language], "answer": policy["payment_boundary"],
        }, {
            "question": _DELAY_QUESTION[language], "answer": policy["delivery_delay"],
        }]
    elif page_key == "returns":
        # Remove the categorical custom exclusion while retaining the practical
        # item-condition and contact instructions around it.
        page["hero_intro"] = service
        section = _one(page.get("sections", []), "title", labels["returns_section"], "returns.terms_section")
        # No condition-of-item bullet here: it belongs to ordinary voluntary
        # change-of-mind review and must not operate as a defect barrier.
        section["bullets"] = [service]
        service_faq = _one(page.get("faq_items", []), "question", labels["returns_service_question"], "returns.service_faq")
        service_faq["answer"] = service
        # The old detailed FAQ invented refund deadlines, cost coverage and
        # reservations. Publish the two supported answers, without those claims.
        page["faq_items"] = [service_faq, {
            "question": _SERVICE_QUESTION[language], "answer": policy["service_request"],
        }]
    elif page_key == "help_center":
        # The fourth support section and its dedicated service FAQ duplicate
        # the former categorical custom rule.
        section = _one(page.get("sections", []), "title", labels["help_section"], "help.service_section")
        section["paragraphs"] = [service]
        _one(page.get("faq_items", []), "question", labels["help_service_question"], "help.service_faq")["answer"] = service
    elif page_key == "faq":
        _one(page.get("faq_items", []), "question", labels["faq_delivery_question"], "faq.delivery")["answer"] = dispatch
        _one(page.get("faq_items", []), "question", labels["faq_service_question"], "faq.service")["answer"] = service
        for item in page.get("faq_items", []):
            question = str(item.get("question"))
            if question == "Як оформити замовлення на TwoComms?":
                item["answer"] = (
                    "Оберіть товар, розмір і колір, додайте позицію в кошик, "
                    "заповніть контакти, доставку та оплату. " + dispatch
                )
            elif question == "Що робити, якщо посилка не прийшла у строк?":
                item["answer"] = policy["delivery_delay"]
        page["faq_items"] = [item for item in page.get("faq_items", []) if str(item.get("question")) not in {
            "Чи доступна кур’єрська доставка у день замовлення?",
            "Чи доступна кур’єрська доставка?",
        }]

    return page
