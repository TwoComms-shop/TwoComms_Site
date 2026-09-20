"""Э2.10 — единый объявленный бюджет хода, из которого выводятся все таймауты.

**Проблема, которую это устраняет.** Константы задавались независимо друг от
друга, и их сумма превышала окно живости демона:

    HB_ALIVE_WINDOW = 45 с
    бюджет сложного хода = 45 (Gemini) + 3 (typing) + 4×12 (отправка) ≈ 96 с

То есть штатный сложный ответ гарантированно превышал окно вдвое. Ложное
срабатывание watchdog было не редким случаем, а **ожидаемым поведением**.

Второе следствие серьёзнее шума в логах: restart в середине долгого хода. Если
процесс перезапускается после провайдерского вызова, но до записи receipt,
результат отправки становится **неизвестным**. То есть ложное срабатывание
watchdog способно создать реальную неопределённость доставки для клиента.

**Решение.** Один объявленный бюджет с разложением на фазы. Окно живости
**выводится** из бюджета, а не задаётся независимо. Тест согласованности ломается,
если сумма фаз перестаёт укладываться в бюджет — то есть изменить один таймаут и
не заметить нарушение больше нельзя.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TurnPhase:
    """Одна фаза хода с объявленным максимумом в секундах."""

    name: str
    max_seconds: float
    note: str = ""


# Фазы одного логического хода клиента, худший случай каждой.
# Числа берутся из фактических констант, а не назначаются здесь: смысл модуля в
# том, чтобы связать их между собой, а не создать второй источник истины.
def turn_phases() -> tuple:
    from management.services.call_ai_analysis import (
        CHAT_COMPLEX_DEADLINE_SECONDS,
        CHAT_ORDINARY_DEADLINE_SECONDS,
    )
    from management.services.instagram_bot import HTTP_TIMEOUT
    from management.services.ig_delivery_plan import DEFAULT_MAX_CHUNKS

    return (
        TurnPhase(
            "turn_debounce",
            _turn_debounce_seconds(),
            "ожидание burst клиента (Э2.2); ход ещё не начал работу",
        ),
        TurnPhase(
            "generation",
            float(CHAT_COMPLEX_DEADLINE_SECONDS),
            f"худший случай: complex-задача; ordinary = {CHAT_ORDINARY_DEADLINE_SECONDS}",
        ),
        # Presence runs independently and never delays the reply. Its session
        # lifetime is not a synchronous phase of the customer turn.
        TurnPhase(
            "delivery",
            float(DEFAULT_MAX_CHUNKS) * float(HTTP_TIMEOUT),
            f"{DEFAULT_MAX_CHUNKS} чанка × {HTTP_TIMEOUT} с на запрос Meta",
        ),
    )


def _turn_debounce_seconds() -> float:
    """Фаза очікування = ФАКТИЧНИЙ максимум склейки, а не оголошена межа.

    Э2.2B prerequisite. Раніше тут читався `MAX_TURN_WAIT` (20 с), тоді як
    `window_deadline` завжди дорівнює `min(now + TURN_DEBOUNCE, now +
    MAX_TURN_WAIT)` і не продовжується при attach — тобто реальне очікування 6 с,
    а `MAX_TURN_WAIT` мертвий. Розходження було не косметичним: воно завищувало
    `customer_notice_threshold_seconds()` на ті самі 14 с і настільно ж знижувало
    чутливість вікна живості демона.

    Тепер джерело істини одне — `ig_customer_turns.effective_max_wait_seconds()`.
    Фіксується зв'язок, а не число: коли з'явиться typed wait policy, значення
    поїде разом з нею без правки цього модуля.
    """
    try:
        from management.services.ig_customer_turns import effective_max_wait_seconds

        return float(effective_max_wait_seconds())
    except Exception:
        return 0.0


def declared_turn_budget_seconds() -> float:
    """Сумма максимумов всех фаз — объявленный бюджет одного хода."""
    return sum(phase.max_seconds for phase in turn_phases())


# Запас поверх бюджета: процесс может быть живым и при этом на секунду позже
# обновить пульс (GC, диск, шум shared-хостинга). Без запаса тест согласованности
# был бы верным, а поведение — на грани.
HEARTBEAT_SAFETY_MARGIN_SECONDS = 20.0


def heartbeat_alive_window_seconds() -> int:
    """Окно живости, ВЫВЕДЕННОЕ из бюджета хода, а не заданное независимо.

    Именно это соотношение и было нарушено: окно 45 с при бюджете ~96 с означало,
    что штатный долгий ход выглядит мёртвым.
    """
    return int(declared_turn_budget_seconds() + HEARTBEAT_SAFETY_MARGIN_SECONDS)


def customer_notice_threshold_seconds() -> float:
    """Момент, раньше которого технический текст клиенту вреден (ЭБ.1).

    Порог — сумма фаз, которые клиент проводит в ожидании **ответа**: склейка
    его сообщений плюс генерация. Доставку сюда не включаем: если мы дошли до
    доставки, у нас есть что сказать, и техтекст уже не нужен.

    Почему не отдельная константа: любое независимое число здесь разошлось бы с
    реальным дедлайном генерации при первой же его правке — ровно та ошибка,
    из-за которой окно живости демона было меньше бюджета хода (Э2.10). Пока
    индикатор набора жив (`_TypingPulse`), молчание читается как «нам пишут», а
    не как поломка, поэтому ждать до конца заявленного бюджета безопасно.
    """
    phases = {phase.name: phase.max_seconds for phase in turn_phases()}
    return float(phases.get("turn_debounce", 0.0) + phases.get("generation", 0.0))


def budget_report() -> dict:
    """Читаемый разбор бюджета — для теста согласованности и для оператора."""
    phases = turn_phases()
    budget = declared_turn_budget_seconds()
    return {
        "phases": [
            {"name": phase.name, "max_seconds": phase.max_seconds, "note": phase.note}
            for phase in phases
        ],
        "declared_budget_seconds": budget,
        "heartbeat_alive_window_seconds": heartbeat_alive_window_seconds(),
        "safety_margin_seconds": HEARTBEAT_SAFETY_MARGIN_SECONDS,
    }
