"""Safe, deterministic text payloads for the durable commerce outbox."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from management.services.ig_commerce_types import CommerceTurnRequest


_CLARIFICATIONS = {
    "multiple_product_links": (
        "Бачу кілька товарів. Надішліть, будь ласка, одне посилання на потрібний варіант."
    ),
    "which_product": (
        "Уточніть, будь ласка, який саме товар ви маєте на увазі, або надішліть посилання на нього."
    ),
    "print_placement": (
        "Уточніть, будь ласка, де саме має бути без принта: спереду, на спині чи всюди."
    ),
    "new_purchase_or_exchange": (
        "Уточніть, будь ласка, це нове замовлення чи обмін уже отриманого товару."
    ),
}


def _payload(text: str) -> dict:
    return {"text": [text]}


def build_durable_reply_payload(
    request: CommerceTurnRequest,
    *,
    action: str,
    reasons: Sequence[str],
    before: Mapping,
    after: Mapping,
) -> dict:
    """Return a persistable single-chunk reply for a safe commerce outcome.

    This layer must stay independent from mutable catalog, price, availability,
    payment, and staffing data. More expressive candidate and checkout replies
    are delivered by later W9 stages only after their facts are authoritative.
    """
    del before  # The interface deliberately keeps a deterministic state snapshot.
    reason_set = {str(reason) for reason in reasons}
    if action == "candidate_rejected" and "candidate_prompt_mismatch" in reason_set:
        return _payload(
            "Ця добірка вже неактуальна. Виберіть варіант з останнього повідомлення "
            "або надішліть посилання на товар."
        )
    if action == "clarification_requested":
        clarification = str(
            after.get("pending_clarification") or request.pending_clarification or ""
        )
        text = _CLARIFICATIONS.get(clarification)
        if text:
            return _payload(text)
    if action == "product_selected" and request.exact_product_id:
        return _payload(
            "Зафіксувала цей варіант. Підкажіть, будь ласка, розмір, колір і кількість."
        )
    return {}


def missing_selector_question(selector, *, language="uk", label=""):
    """One question for the captured missing selector; no stock/effect claims."""
    copy = {
        "product": {"uk": "Яку саме модель або принт ви хочете? Можете надіслати посилання.",
                    "ru": "Какую именно модель или принт вы хотите? Можете прислать ссылку.",
                    "en": "Which model or print would you like? You can send its link."},
        "size": {"uk": "Який розмір ви обираєте?", "ru": "Какой размер вы выбираете?", "en": "Which size would you like?"},
        "fit": {"uk": "Яку посадку ви обираєте?", "ru": "Какую посадку вы выбираете?", "en": "Which fit would you like?"},
        "color": {"uk": "Який колір ви обираєте?", "ru": "Какой цвет вы выбираете?", "en": "Which colour would you like?"},
        "quantity": {"uk": "Яка кількість вам потрібна?", "ru": "Какое количество вам нужно?", "en": "How many would you like?"},
        "option": {"uk": "Який варіант цієї опції ви обираєте?", "ru": "Какой вариант этой опции вы выбираете?", "en": "Which option would you like?"},
    }
    key = str(selector).split(":", 1)[0]
    if key == "option" and label:
        name = " ".join(str(label).split())[:80]
        return {"uk": f"Який варіант опції «{name}» ви обираєте?",
                "ru": f"Какой вариант опции «{name}» вы выбираете?",
                "en": f"Which variant of “{name}” would you like?"}.get(language, f"Який варіант опції «{name}» ви обираєте?")
    return copy.get(key, {}).get(language, copy.get(key, {}).get("uk", ""))
