"""Reviewed public facts used by TwoComms conversational consumers.

This is deliberately a small, versioned runtime source.  Repository Markdown
remains an editable archive, but it is not a provider input: publishing a
business rule belongs to the instruction-publication flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from management.services.ig_core_policy import ORDINARY_DISPATCH_WINDOW_DAYS


APPROVED_PUBLIC_FACTS_VERSION = "2026-10-17.tshirt-fit-audience.1"
SUPPORTED_PUBLIC_FACT_LANGUAGES = ("uk", "ru", "en")


@dataclass(frozen=True)
class ApprovedPublicFact:
    """One typed fact with separate public and provider renderings."""

    key: str
    public_text: str
    scope: str
    provider_directive: str = ""

    @property
    def body(self) -> str:
        """Provider wording: public fact followed by its usage boundary."""
        return "\n".join(part for part in (self.public_text, self.provider_directive) if part)


def _dispatch_days() -> str:
    """Render the one canonical ordinary-dispatch range for all languages."""
    start, end = ORDINARY_DISPATCH_WINDOW_DAYS
    return f"{start}–{end}"


def _render_fact(fact: ApprovedPublicFact) -> ApprovedPublicFact:
    """Resolve shared dynamic facts without maintaining translated number copies."""
    return ApprovedPublicFact(
        key=fact.key,
        public_text=fact.public_text.replace("{dispatch_days}", _dispatch_days()),
        scope=fact.scope,
        provider_directive=fact.provider_directive.replace("{dispatch_days}", _dispatch_days()),
    )


# These facts apply only to the current named bases.  They are intentionally
# not a promise about a later batch, an unlisted product, or a custom order.
_FACTS: dict[str, tuple[ApprovedPublicFact, ...]] = {
    "uk": (
        ApprovedPublicFact(
            "brand",
            "TwoComms — український бренд одягу. Назва має два смисли: кома "
            "як важкий стан і кома як розділовий знак, що продовжує думку. "
            "Знак shaka — дружнє привітання, пов’язане з історією засновника.",
            "public_brand",
        ),
        ApprovedPublicFact(
            "assistant",
            "Соломія — віртуальна помічниця TwoComms у Direct. Вона чесно "
            "відповідає на пряме запитання про це й допомагає з підтвердженими "
            "товарами та умовами.",
            "public_assistant_identity",
        ),
        ApprovedPublicFact(
            "current_tshirt_bases",
            "Для поточних основ футболок: regular — 190 г/м², 95% бавовни, "
            "5% еластану, кулірка пенье; oversize — 210–220 г/м² залежно від "
            "основи, з вільнішими проймою, рукавом і горловиною.",
            "current_regular_and_oversize_tshirt_bases",
            "Називай ці дані лише для відповідної підтвердженої основи з каталогу.",
        ),
        ApprovedPublicFact(
            "current_tshirt_audience",
            "Поточні футболки TwoComms у класичному крої (regular) та oversize — "
            "унісекс: їх носять і жінки, і чоловіки. Класика має звичну посадку, "
            "oversize — вільнішу; крій обирають за бажаним силуетом.",
            "current_regular_and_oversize_tshirt_bases",
            "Правило підтверджене власником магазину 17.10.2026 для цих основ футболок. "
            "Прив'язуй його до підтвердженої футболки; не поширюй на весь каталог чи майбутні основи. "
            "Питання про жіночу або чоловічу посадку не змінює вибраний принт. "
            "Крій не підтверджує наявність, розмір або фото кожного варіанта.",
        ),
        ApprovedPublicFact(
            "unconfirmed_bases",
            "Поточні худі мають фліс, але їхній склад і щільність публічно "
            "не підтверджені. Склад і щільність лонгсліва потребують "
            "специфікації вибраної основи. Догляд береться з ярлика або "
            "підтвердженої картки конкретного товару.",
            "hoodie_and_longsleeve_fact_boundary",
            "Не називай склад або щільність без специфікації вибраної основи.",
        ),
        ApprovedPublicFact(
            "dispatch",
            "Зазвичай готуємо та відправляємо замовлення протягом {dispatch_days} днів "
            "після підтвердження оплати. Це не гарантія і не строк перевезення: "
            "доставка після відправлення залежить від Нової Пошти та маршруту.",
            "ordinary_dispatch_after_confirmed_payment",
        ),
        ApprovedPublicFact(
            "service_request",
            "Для сервісного звернення повідомте номер замовлення, коротко опишіть "
            "ситуацію та бажаний результат. За потреби додайте фото. Команда "
            "перевірить деталі й погодить подальші кроки, строки та умови пересилання.",
            "service_intake_without_automatic_remedy_or_deadline",
        ),
        ApprovedPublicFact(
            "delivery_delay",
            "Якщо відправлення затримується, зверніться до команди з номером "
            "замовлення або ТТН. Перевіримо доступний статус і уточнимо подальші дії; "
            "точний строк залежить від перевізника та конкретної ситуації.",
            "delivery_issue_status_and_team_review",
        ),
        ApprovedPublicFact(
            "service_boundary",
            "Для каталожних товарів після отримання діє орієнтир 14 днів для "
            "звичайного обміну або повернення за застосовними умовами та після розгляду "
            "командою. Custom і витрачені бали можуть впливати на звичайну "
            "добровільну зміну рішення, але не є автоматичною відмовою у "
            "зверненні про дефект, невідповідність або обов’язкові права покупця.",
            "catalogue_service_policy_with_defect_boundary",
        ),
        ApprovedPublicFact(
            "payment_boundary",
            "Умови оплати, знижок, балів і кастома підтверджуються для "
            "конкретного checkout або погодженого менеджером сценарію.",
            "current_checkout_and_manager_approval_only",
            "Не вигадуй фіксовану передоплату, знижку чи право на винагороду.",
        ),
    ),
    "ru": (
        ApprovedPublicFact(
            "brand",
            "TwoComms — украинский бренд одежды. У названия два смысла: кома "
            "как тяжёлое состояние и запятая как знак препинания, продолжающий "
            "мысль. Знак shaka — дружеское приветствие, связанное с историей основателя.",
            "public_brand",
        ),
        ApprovedPublicFact(
            "assistant",
            "Соломия — виртуальный помощник TwoComms в Direct. Она честно "
            "отвечает на прямой вопрос об этом и помогает с подтверждёнными "
            "товарами и условиями.",
            "public_assistant_identity",
        ),
        ApprovedPublicFact(
            "current_tshirt_bases",
            "Для текущих основ футболок: regular — 190 г/м², 95% хлопка, "
            "5% эластана, кулирка пенье; oversize — 210–220 г/м² в зависимости "
            "от основы, с более свободными проймой, рукавом и горловиной.",
            "current_regular_and_oversize_tshirt_bases",
            "Называй эти данные только для соответствующей подтверждённой основы из каталога.",
        ),
        ApprovedPublicFact(
            "current_tshirt_audience",
            "Текущие футболки TwoComms классического кроя (regular) и oversize — "
            "унисекс: их носят и женщины, и мужчины. Классика имеет привычную посадку, "
            "oversize — более свободную; крой выбирают по желаемому силуэту.",
            "current_regular_and_oversize_tshirt_bases",
            "Правило подтверждено владельцем магазина 17.10.2026 для этих основ футболок. "
            "Привязывай его к подтверждённой футболке; не распространяй на весь каталог или будущие основы. "
            "Вопрос о женской или мужской посадке не меняет выбранный принт. "
            "Крой не подтверждает наличие, размер или фото каждого варианта.",
        ),
        ApprovedPublicFact(
            "unconfirmed_bases",
            "Текущие худи на флисе, но их состав и плотность публично не "
            "подтверждены. Состав и плотность лонгслива требуют спецификации "
            "выбранной основы. Уход берётся с ярлыка или подтверждённой карточки "
            "конкретного товара.",
            "hoodie_and_longsleeve_fact_boundary",
            "Не называй состав или плотность без спецификации выбранной основы.",
        ),
        ApprovedPublicFact(
            "dispatch",
            "Обычно готовим и отправляем заказы в течение {dispatch_days} дней после "
            "подтверждения оплаты. Это не гарантия и не срок перевозки: доставка "
            "после отправки зависит от Новой Почты и маршрута.",
            "ordinary_dispatch_after_confirmed_payment",
        ),
        ApprovedPublicFact(
            "service_request",
            "Для сервисного обращения сообщите номер заказа, кратко опишите "
            "ситуацию и желаемый результат. При необходимости добавьте фото. Команда "
            "проверит детали и согласует дальнейшие шаги, сроки и условия пересылки.",
            "service_intake_without_automatic_remedy_or_deadline",
        ),
        ApprovedPublicFact(
            "delivery_delay",
            "Если отправление задерживается, обратитесь к команде с номером "
            "заказа или ТТН. Проверим доступный статус и уточним дальнейшие действия; "
            "точный срок зависит от перевозчика и конкретной ситуации.",
            "delivery_issue_status_and_team_review",
        ),
        ApprovedPublicFact(
            "service_boundary",
            "Для товаров каталога после получения действует ориентир 14 дней для "
            "обычного обмена или возврата по применимым условиям и после рассмотрения "
            "командой. Custom и потраченные баллы могут влиять на обычную "
            "добровольную смену решения, но не являются автоматическим отказом "
            "в обращении о дефекте, несоответствии или обязательных правах покупателя.",
            "catalogue_service_policy_with_defect_boundary",
        ),
        ApprovedPublicFact(
            "payment_boundary",
            "Условия оплаты, скидок, баллов и кастома подтверждаются для "
            "конкретного checkout или согласованного менеджером сценария.",
            "current_checkout_and_manager_approval_only",
            "Не придумывай фиксированную предоплату, скидку или право на вознаграждение.",
        ),
    ),
    "en": (
        ApprovedPublicFact(
            "brand",
            "TwoComms is a Ukrainian clothing brand. Its name has two meanings: "
            "coma as a serious condition and comma as punctuation that continues "
            "a thought. The shaka mark is a friendly greeting connected with the "
            "founder's story.",
            "public_brand",
        ),
        ApprovedPublicFact(
            "assistant",
            "Solomiia is TwoComms' virtual Direct assistant. She answers a "
            "direct question about this honestly and helps with confirmed "
            "products and terms.",
            "public_assistant_identity",
        ),
        ApprovedPublicFact(
            "current_tshirt_bases",
            "For current T-shirt bases: regular is 190 g/m², 95% cotton and "
            "5% elastane, combed jersey; oversize is 210–220 g/m² depending on "
            "the base, with a roomier armhole, sleeve and neckline.",
            "current_regular_and_oversize_tshirt_bases",
            "State these facts only for the matching confirmed catalogue base.",
        ),
        ApprovedPublicFact(
            "current_tshirt_audience",
            "Current TwoComms classic (regular) and oversize T-shirts are unisex: "
            "women and men wear the same styles. Classic has a regular fit; "
            "oversize is roomier. Choose the cut for the silhouette you prefer.",
            "current_regular_and_oversize_tshirt_bases",
            "The shop owner confirmed this rule on 2026-10-17 for these T-shirt bases. "
            "Bind it to a confirmed T-shirt, not the entire catalogue or future bases. "
            "A women's or men's fit question does not change the chosen print. "
            "Cut does not prove stock, size or a photo of every variant.",
        ),
        ApprovedPublicFact(
            "unconfirmed_bases",
            "Current hoodies have fleece, but their composition and weight are "
            "not publicly confirmed. A long-sleeve composition and weight require "
            "the chosen base specification. Care comes from the label or a "
            "confirmed product card.",
            "hoodie_and_longsleeve_fact_boundary",
            "Do not state a composition or weight without the chosen base specification.",
        ),
        ApprovedPublicFact(
            "dispatch",
            "We usually prepare and dispatch orders within {dispatch_days} days after "
            "payment is confirmed. This is not a guarantee or a transit time: "
            "delivery after dispatch depends on Nova Poshta and the route.",
            "ordinary_dispatch_after_confirmed_payment",
        ),
        ApprovedPublicFact(
            "service_request",
            "For a service request, share your order number, briefly describe "
            "the situation and your preferred outcome, and add a photo if helpful. "
            "The team will review the details and agree on next steps, timing and shipping terms.",
            "service_intake_without_automatic_remedy_or_deadline",
        ),
        ApprovedPublicFact(
            "delivery_delay",
            "If a shipment is delayed, contact the team with your order or tracking "
            "number. We will check the available status and clarify next steps; "
            "exact timing depends on the carrier and the situation.",
            "delivery_issue_status_and_team_review",
        ),
        ApprovedPublicFact(
            "service_boundary",
            "After receipt, catalogue items have a 14-day guideline for ordinary "
            "returns or exchanges under applicable terms and team review. Custom work and "
            "spent points can affect an ordinary voluntary change of mind, but "
            "never automatically reject a defect, mismatch, or mandatory consumer-rights request.",
            "catalogue_service_policy_with_defect_boundary",
        ),
        ApprovedPublicFact(
            "payment_boundary",
            "Payment, discount, points and custom terms are confirmed for the "
            "specific checkout or a manager-approved scenario.",
            "current_checkout_and_manager_approval_only",
            "Do not invent a fixed deposit, discount, or reward entitlement.",
        ),
    ),
}


def approved_public_facts(language: str) -> tuple[ApprovedPublicFact, ...]:
    """Return facts for a supported public language, without fallback mixing."""
    try:
        return tuple(_render_fact(fact) for fact in _FACTS[language])
    except KeyError as exc:
        raise ValueError(f"unsupported public-fact language: {language}") from exc


def approved_public_fact_manifest(language: str) -> dict:
    """Return a stable, non-secret publication identity for one language."""
    facts = approved_public_facts(language)
    payload = {
        "version": APPROVED_PUBLIC_FACTS_VERSION,
        "language": language,
        "facts": [
            {
                "key": fact.key,
                "public_text": fact.public_text,
                "provider_directive": fact.provider_directive,
                "scope": fact.scope,
            }
            for fact in facts
        ],
    }
    return {
        "version": APPROVED_PUBLIC_FACTS_VERSION,
        "language": language,
        "content_hash": sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "facts": facts,
    }


def approved_public_fragment(language: str = "uk", *, keys: tuple[str, ...] | None = None) -> str:
    """Clean user-facing copy for site and checkout consumers.

    It is rendered from the same fact objects as the provider modules, but
    deliberately excludes directives such as source or capability guards.
    """
    facts = approved_public_facts(language)
    if keys is not None:
        wanted = set(keys)
        facts = tuple(fact for fact in facts if fact.key in wanted)
        missing = wanted.difference(fact.key for fact in facts)
        if missing:
            raise KeyError(f"unknown approved public fact key: {sorted(missing)!r}")
    return "\n\n".join(fact.public_text for fact in facts)


def approved_provider_fact_definitions(language: str = "uk") -> tuple[tuple[str, str, int], ...]:
    """Provider-safe modules for the selected customer language.

    The current compiler defaults to Ukrainian.  A future language-aware
    consumer can pass ``ru`` or ``en`` without ever reading the Markdown
    archive or translating an approved statement on the fly.
    """
    facts = approved_public_facts(language)
    return tuple(
        (f"approved_public:{language}:{fact.key}", fact.body, priority)
        for priority, fact in enumerate(facts)
    )


def approved_provider_fact_text(key: str, language: str = "uk") -> str:
    """Return one reviewed provider fact without exposing archive Markdown."""
    for fact in approved_public_facts(language):
        if fact.key == key:
            return fact.body
    raise KeyError(f"unknown approved public fact key: {key}")
