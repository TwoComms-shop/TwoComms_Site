"""Reviewed public facts used by TwoComms conversational consumers.

This is deliberately a small, versioned runtime source.  Repository Markdown
remains an editable archive, but it is not a provider input: publishing a
business rule belongs to the instruction-publication flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re

from management.services.ig_core_policy import ORDINARY_DISPATCH_WINDOW_DAYS


APPROVED_PUBLIC_FACTS_VERSION = "2026-10-18.owner-scoped.1"
SUPPORTED_PUBLIC_FACT_LANGUAGES = ("uk", "ru", "en")


@dataclass(frozen=True)
class ApprovedPublicFact:
    """One typed fact with separate public and provider renderings."""

    key: str
    public_text: str
    scope: str
    provider_directive: str = ""
    topic: str = "general"
    base_keys: tuple[str, ...] = ()
    source_ref: str = "reviewed:canonical-public-facts"
    review_status: str = "reviewed"
    version: str = APPROVED_PUBLIC_FACTS_VERSION

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
        topic=fact.topic, base_keys=fact.base_keys, source_ref=fact.source_ref,
        review_status=fact.review_status, version=fact.version,
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
    ),
}


def approved_public_facts(language: str) -> tuple[ApprovedPublicFact, ...]:
    """Return facts for a supported public language, without fallback mixing."""
    try:
        from management.knowledge import bases, care_quality, payment

        from dataclasses import replace

        topics = {"brand": "brand", "assistant": "identity", "current_tshirt_audience": "fit",
                  "dispatch": "dispatch", "service_request": "service", "delivery_delay": "service",
                  "service_boundary": "service"}
        canonical = {fact.key: replace(fact, topic=topics.get(fact.key, fact.topic)) for fact in _FACTS[language]}
        for module in (bases, care_quality, payment):
            for key, scope, topic, base_keys, translations, boundary in module.FACTS:
                canonical[key] = ApprovedPublicFact(key, translations[language], scope, boundary,
                    topic, base_keys, module.SOURCE, module.REVIEW, module.VERSION)
        return tuple(_render_fact(fact) for fact in canonical.values())
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
                "topic": fact.topic, "base_keys": list(fact.base_keys),
                "source_ref": fact.source_ref, "review_status": fact.review_status,
                "version": fact.version,
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


_TOPICS = {
    "composition": re.compile(r"состав\w*|склад(?:\s+тканин\w*)?\b|composition|fabric\s+content", re.I),
    "material": re.compile(r"матер[іи]ал\w*|тканин\w*|fabric|material", re.I),
    "density": re.compile(r"щільн\w*|плотност\w*|density|gsm|г/м", re.I),
    "care": re.compile(r"догляд\w*|ухаж\w*|уход|прати|стират\w*|wash|care|температур\w*", re.I),
    "quality": re.compile(r"якіст\w*|качеств\w*|quality|скільки\s+пран\w*|сколько\s+стир\w*|тріск\w*|треск\w*", re.I),
    "brand": re.compile(r"бренд\w*|brand|twocomms|засновник\w*|основател\w*|founder", re.I),
    "identity": re.compile(r"ти\s+бот|ви\s+бот|ты\s+бот|вы\s+бот|are\s+you\s+(?:a\s+)?bot|хто\s+ти|кто\s+ты|who\s+are\s+you", re.I),
    "fit": re.compile(r"посадк\w*|крій|крою|крой|фасон\w*|ун[іи]секс|unisex|\bfit\b|oversize|regular|жіноч\w*|женск\w*|чоловіч\w*|мужск\w*", re.I),
    "payment": re.compile(r"оплат\w*|передоплат\w*|предоплат\w*|післяплат\w*|налож\w*|payment|deposit|cash\s+on\s+delivery", re.I),
    "dispatch": re.compile(r"відправ\w*|отправ\w*|достав\w*|dispatch|shipping|delivery", re.I),
    "service": re.compile(r"обмін\w*|обмен\w*|повернен\w*|возврат\w*|брак\w*|дефект\w*|exchange|return|defect", re.I),
}
_UNSCOPED = re.compile(r"майбут\w*|будущ\w*|наступн\w*\s+парт\w*|нов\w*\s+парт\w*|future|next\s+batch|coming\s+collection", re.I)
_HARDWARE = re.compile(r"манжет\w*|люверс\w*|фурн[іи]тур\w*|резинк\w*|ґудзик\w*|пуговиц\w*|блискавк\w*|молни\w*|cuffs?|eyelets?|grommets?|ribbing|buttons?|zipper", re.I)
_GARMENT_WORDS = {
    "tshirt": re.compile(r"футбол\w*|t[- ]?shirts?", re.I),
    "hoodie": re.compile(r"\b(?:худі|худи|hoodies?)\b", re.I),
    "longsleeve": re.compile(r"лонгсл\w*|longsleeve\w*|long[- ]sleeve", re.I),
    "zip_hoodie": re.compile(r"зіп\w*|зип\w*|zip[- ]?hoodie", re.I),
}


def knowledge_topics(sources):
    """Finite lexical selectors over own source text; never factual authority."""
    from management.services.ig_reply_language import own_reply_text

    text = "\n".join(own_reply_text(str(row.get("text") or "")) for row in sources or ()
                     if row.get("role", "user") == "user")
    return tuple(key for key, pattern in _TOPICS.items() if pattern.search(text))


def _captured_base(readiness, question):
    """A current catalogue category is proof; a customer/model noun is not."""
    from management.knowledge.bases import BASES

    product = (readiness or {}).get("product") or {}
    if (type(product.get("id")) is not int or product["id"] <= 0
        or _UNSCOPED.search(question) or _HARDWARE.search(question)
        or product.get("is_future") or product.get("is_archived")
        or product.get("base_version") not in (None, "", "current", "2026-10-18")):
        return "", ""
    kind = str(product.get("category_slug") or product.get("kind") or "").strip().casefold()
    garment = {"tshirts": "tshirt", "tshirt": "tshirt", "футболки": "tshirt", "футболка": "tshirt",
               "hoodies": "hoodie", "hoodie": "hoodie", "худі": "hoodie", "худи": "hoodie"}.get(kind, "")
    # The inquiry may explicitly concern a different garment from the cart.
    mentioned = {key for key, pattern in _GARMENT_WORDS.items() if pattern.search(question)}
    if not garment or mentioned - {garment}:
        return "", ""
    if garment == "hoodie":
        return "current_hoodie", garment
    fit = str(((readiness or {}).get("fit") or {}).get("selected") or "").casefold()
    if not fit:
        options = ((readiness or {}).get("fit") or {}).get("options") or []
        if len(options) == 1 and isinstance(options[0], dict):
            fit = str(options[0].get("code") or "").casefold()
    fit = {"classic": "regular", "regular": "regular", "oversize": "oversize"}.get(fit, "")
    key = "current_tshirt_" + fit if fit else "current_tshirt"
    return (key, garment) if key in BASES or key == "current_tshirt" else ("", "")


def requested_product_topics(question):
    """Separate requested facts; a fabric noun in composition is not a second ask."""
    topics = set(knowledge_topics([{"text": str(question or "")}]))
    if "composition" in topics and "material" in topics:
        if not re.search(r"матер[іи]ал\w*|\band\s+(?:the\s+)?fabric\b|(?:і|и|та)\s+тканин\w*", str(question), re.I):
            topics.discard("material")
    return tuple(key for key in ("composition", "material", "density", "care", "quality") if key in topics)


def approved_base_assertions(readiness, *, question, language="uk"):
    return tuple(assertion for topic in requested_product_topics(question)
        if (assertion := approved_base_assertion(readiness, question=question, language=language, topic=topic)) is not None)


def approved_base_assertion(readiness, *, question, language="uk", topic=None):
    """Resolve one topic against a captured base, without DB/provider effects.

    The returned assertion is shared by inquiry, prompt, fallback, coverage and
    truth validation. It conveys no price, stock, selection or checkout right.
    """
    from management.knowledge.bases import BASES

    if language not in SUPPORTED_PUBLIC_FACT_LANGUAGES:
        return None
    base_key, garment = _captured_base(readiness, str(question or ""))
    topics = requested_product_topics(question)
    topic = next(iter(topics), "") if topic is None else topic if topic in topics else ""
    if not base_key or not topic or (base_key == "current_tshirt" and topic == "density"):
        return None
    fact_key = ("current_print_quality" if topic == "quality" else "current_care" if topic == "care"
                else "current_hoodie_bases" if garment == "hoodie" else "current_tshirt_bases")
    published_facts = approved_public_facts(language)
    fact = next((item for item in published_facts if item.key == fact_key), None)
    if fact is None or not fact.review_status.startswith("reviewed"):
        return None
    base = BASES.get(base_key, BASES["current_tshirt_regular"])
    text = fact.public_text
    if topic == "composition":
        text = {
            "uk": "Склад цієї основи: " + ("90% бавовни / 10% поліестеру." if garment == "hoodie" else "95% бавовни / 5% еластану."),
            "ru": "Состав этой основы: " + ("90% хлопка / 10% полиэстера." if garment == "hoodie" else "95% хлопка / 5% эластана."),
            "en": "This base is " + ("90% cotton / 10% polyester." if garment == "hoodie" else "95% cotton / 5% elastane."),
        }[language]
    elif topic == "density":
        text = {"uk": f"Щільність цієї основи — близько {base['gsm']} г/м².",
                "ru": f"Плотность этой основы — около {base['gsm']} г/м².",
                "en": f"This base is approximately {base['gsm']} g/m²."}[language]
    elif topic == "material":
        text = {"uk": "Тринитка пенье з флісом." if garment == "hoodie" else "Турецька кулірка пенье, 95% бавовни / 5% еластану.",
                "ru": "Трёхнитка пенье с флисом." if garment == "hoodie" else "Турецкая кулирка пенье, 95% хлопка / 5% эластана.",
                "en": "Combed three-thread fleece." if garment == "hoodie" else "Turkish combed kulir jersey, 95% cotton / 5% elastane."}[language]
    elif topic == "care":
        text = {"uk": "Радимо прати при 30°C, дотримуючись рекомендацій догляду, без агресивної хімії.",
                "ru": "Рекомендуем стирать при 30°C, соблюдая рекомендации ухода, без агрессивной химии.",
                "en": "Wash at 30°C, follow the care guidance and avoid harsh chemicals."}[language]
    assertion = {"schema": "approved-base-assertion.v1", "fact_key": fact_key, "fact_version": fact.version,
        "publication_version": APPROVED_PUBLIC_FACTS_VERSION, "source_ref": fact.source_ref,
        "review_status": fact.review_status, "base_key": base_key, "garment": garment,
        "product_id": readiness["product"]["id"], "topic": topic, "language": language,
        "text": text, "question": str(question)[:500], "composition": [list(row) for row in base["composition"]],
        "available_fact_versions": {item.key: item.version for item in published_facts
            if item.key in {"current_care", "current_print_quality"} and item.review_status.startswith("reviewed")
            and (base_key in item.base_keys or base_key == "current_tshirt" and "current_tshirt_regular" in item.base_keys)},
        "gsm": base.get("gsm") if base_key != "current_tshirt" else None,
        "catalog_authority": False, "checkout_authority": False}
    assertion["digest"] = sha256(json.dumps(assertion, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return assertion


def approved_assertion_valid(assertion):
    """Reject stale versions/changed payloads before reusing a sealed assertion."""
    if not isinstance(assertion, dict) or assertion.get("publication_version") != APPROVED_PUBLIC_FACTS_VERSION:
        return False
    if assertion.get("schema") == "approved-category-assertion.v1":
        try:
            expected = approved_category_context(assertion.get("subject"),
                current_catalogue_categories=assertion.get("current_catalogue_categories", ()),
                language=assertion.get("language"))
            return bool(expected and expected[0] == assertion)
        except (KeyError, TypeError, ValueError):
            return False
    bases = {"current_tshirt_regular": ("tshirts", "regular"), "current_tshirt_oversize": ("tshirts", "oversize"),
             "current_tshirt": ("tshirts", ""), "current_hoodie": ("hoodies", "oversize")}
    scope = bases.get(assertion.get("base_key"))
    if scope is None:
        return False
    try:
        expected = approved_base_assertion({"product": {"id": assertion.get("product_id"), "kind": scope[0]},
            "fit": {"selected": scope[1]}}, question=assertion.get("question", ""), language=assertion.get("language"),
            topic=assertion.get("topic"))
        return expected is not None and expected == assertion
    except (KeyError, TypeError, ValueError):
        return False


def approved_base_context(readiness, *, sources=(), language="uk"):
    """Base proof for a captured truth context, independent of asked topic.

    Selection scope alone never makes a different/future information subject
    true. This helper performs no selection, read, publication or I/O.
    """
    from management.services.ig_reply_language import own_reply_text

    own = "\n".join(own_reply_text(str(row.get("text") or "")) for row in sources or ()
                    if row.get("role", "user") == "user")
    if not _captured_base(readiness, own)[0]:
        return ()
    assertion = approved_base_assertion(readiness, question="composition", language=language)
    return (assertion,) if assertion else ()


def approved_category_context(subject, *, current_catalogue_categories=(), language="uk"):
    """Reviewed current category facts plus captured published-category proof.

    A category question and catalogue presence permit a category explanation,
    never an assertion about an unknown individual product, SKU or its stock.
    The caller supplies the already captured catalogue categories; no read is
    performed here and raw garment nouns alone do not constitute capability.
    """
    from copy import deepcopy
    from management.knowledge.bases import BASES
    from management.services.ig_product_focus import information_category

    if (not isinstance(subject, dict) or subject.get("schema") != "information-subject.v1"
        or subject.get("status") != "identified" or language not in SUPPORTED_PUBLIC_FACT_LANGUAGES
        or any(subject.get(key) is not False for key in ("future_base_requested", "unsupported_detail_requested", "specific_product_requested"))
        or any(subject.get(key) is not False for key in ("selection_authority", "stock_authority", "checkout_authority"))
        or not isinstance(subject.get("source_message_ids"), (list, tuple)) or not 1 <= len(subject["source_message_ids"]) <= 32
        or any(type(value) is not int or value <= 0 for value in subject["source_message_ids"])
        or not isinstance(current_catalogue_categories, (list, tuple, set, frozenset))
        or len(current_catalogue_categories) > 64 or any(not isinstance(value, str) for value in current_catalogue_categories)):
        return ()
    categories = sorted({information_category(value) for value in current_catalogue_categories})
    category = information_category(subject.get("category_slug"))
    garment = subject.get("garment_type")
    if category not in categories or (category, garment) not in {("hoodies", "hoodie"), ("tshirts", "tshirt")}:
        return ()
    base_key = "current_hoodie" if garment == "hoodie" else "current_tshirt"
    base = BASES["current_hoodie" if garment == "hoodie" else "current_tshirt_regular"]
    keys = ("current_hoodie_bases", "current_hoodie_audience") if garment == "hoodie" else ("current_tshirt_bases", "current_tshirt_audience")
    facts = {fact.key: fact for fact in approved_public_facts(language)}
    if any(key not in facts for key in keys):
        return ()
    prefix = {"uk": "Наші поточні худі" if garment == "hoodie" else "Наші поточні футболки",
              "ru": "Наши текущие худи" if garment == "hoodie" else "Наши текущие футболки",
              "en": "Our current hoodies" if garment == "hoodie" else "Our current T-shirts"}[language]
    text = {
        "uk": (prefix + " — унісекс, одна злегка oversize основа: 90% бавовни / 10% поліестеру, тринитка пенье з флісом, близько 320 г/м²."
               if garment == "hoodie" else prefix + " — унісекс, 95% бавовни / 5% еластану, турецька кулірка пенье; regular — близько 190 г/м², oversize — близько 220 г/м²."),
        "ru": (prefix + " — унисекс, одна слегка oversize основа: 90% хлопка / 10% полиэстера, трёхнитка пенье с флисом, около 320 г/м²."
               if garment == "hoodie" else prefix + " — унисекс, 95% хлопка / 5% эластана, турецкая кулирка пенье; regular — около 190 г/м², oversize — около 220 г/м²."),
        "en": (prefix + " are unisex, with one slightly oversize base: 90% cotton / 10% polyester, combed three-thread fleece, approximately 320 g/m²."
               if garment == "hoodie" else prefix + " are unisex, 95% cotton / 5% elastane, Turkish combed kulir jersey; regular is approximately 190 g/m² and oversize approximately 220 g/m²."),
    }[language]
    composition = {"uk": "90% бавовни / 10% поліестеру" if garment == "hoodie" else "95% бавовни / 5% еластану",
                   "ru": "90% хлопка / 10% полиэстера" if garment == "hoodie" else "95% хлопка / 5% эластана",
                   "en": "90% cotton / 10% polyester" if garment == "hoodie" else "95% cotton / 5% elastane"}[language]
    density = {"uk": "близько 320 г/м²" if garment == "hoodie" else "regular — близько 190 г/м², oversize — близько 220 г/м²",
               "ru": "около 320 г/м²" if garment == "hoodie" else "regular — около 190 г/м², oversize — около 220 г/м²",
               "en": "approximately 320 g/m²" if garment == "hoodie" else "regular approximately 190 g/m², oversize approximately 220 g/m²"}[language]
    answers = {"material": text, "composition": prefix + ": " + composition + ".",
               "density": prefix + ": " + density + "."}
    for topic, key in (("care", "current_care"), ("quality", "current_print_quality")):
        if key in facts:
            # Reuse the reviewed public wording, retaining category attribution
            # in the same sentence as each protected claim.
            body = facts[key].public_text
            if topic == "care":
                body = {"uk": "Радимо прати при 30°C, дотримуючись рекомендацій догляду, без агресивної хімії.",
                        "ru": "Рекомендуем стирать при 30°C, соблюдая рекомендации ухода, без агрессивной химии.",
                        "en": "Wash at 30°C, follow the care guidance and avoid harsh chemicals."}[language]
            answers[topic] = prefix + ": " + body
    assertion = {"schema": "approved-category-assertion.v1", "scope_kind": "current_category",
        "publication_version": APPROVED_PUBLIC_FACTS_VERSION, "fact_key": keys[0], "fact_version": facts[keys[0]].version,
        "fact_keys": list(keys), "source_ref": facts[keys[0]].source_ref, "review_status": facts[keys[0]].review_status,
        "base_key": base_key, "garment": garment, "category_slug": category, "topic": "category", "language": language,
        "subject": deepcopy(subject), "current_catalogue_categories": categories, "text": text, "answer_texts": answers,
        "available_fact_versions": {item.key: item.version for item in facts.values()
            if item.key in {"current_care", "current_print_quality"} and item.review_status.startswith("reviewed")
            and (base_key in item.base_keys or base_key == "current_tshirt" and "current_tshirt_regular" in item.base_keys)},
        "composition": [list(row) for row in base["composition"]], "gsm": base["gsm"] if garment == "hoodie" else None,
        "allowed_gsm": [320] if garment == "hoodie" else [190, 220], "product_id": None,
        "selection_authority": False, "stock_authority": False, "catalog_authority": False,
        "checkout_authority": False, "payment_authority": False}
    assertion["digest"] = sha256(json.dumps(assertion, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return (assertion,)
