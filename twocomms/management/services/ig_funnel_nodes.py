"""Одне канонічне визначення вузла воронки з полями політики (Э4.1).

**Що вже було і що тут не переробляється.** `ig_checkout_readiness.checkout_readiness()`
— це фактичний реєстр обов'язкового: `fit.required = bool(fit_rows)`,
`color.required = len(colors) > 1`, `can_issue_link = not missing`. Він
залишається **authority для payable-готовності**: цей модуль його читає і ніколи
не переписує. Товар з одним крієм уже не породжує питання — це готовий
`not_applicable`, а не «невідомо».

**Чого не було.** Другорядного й контекстного, явних станів, відображення і
причини. `missing = ["size"]` однаково виглядає для «клієнт ще не назвав розмір» і
для «назвав, але в цьому кольорі його немає» — а це різні дії (Э4.2).

**Чому два реєстри не робимо.** Спокуса — окремий реєстр обов'язкового і окремий
контекстного. Сім різниць між ними (область життя, поведінка при reset, хто
заповнює, вплив на follow-up, відображення, поведінка при зміні товару, TTL) —
це **сім полів політики**, а не дві таблиці. Дві таблиці означали б два різні
API проєкції, два різні шляхи інвалідації і неминуче розходження між ними.

**Чому визначення живе в коді, а стан — у БД.** План вимагає статичну перевірку
графа (немає циклів, немає висячих посилань) і **одну** міграцію. Таблиця
визначень не перевіряється статично: цикл з'явився б після `UPDATE` у проді, коли
перевіряти вже нікому. Тому визначення — незмінні dataclass-и, які деплояться з
кодом і валідуються при імпорті й у тестах, а міграція потрібна рівно одна — під
стан (`IgFunnelNodeState`). `IgAnalysisProposal.target_definition_key/version` —
рядки саме тому: analysis посилається на ключі визначень і не знає фізичної
таблиці вузлів.

**Чому цикли забороняються не всюди.** Наївна перевірка «граф ациклічний» ламає
правду про дані: колір відсікає розміри, і **обраний розмір теж відсікає
кольори** (`_color_rows(..., size=size_selected)` віддає `variant_allows_purchase`
з розміром). Взаємна інвалідація — це факт каталогу, а не помилка моделювання.
Тому ациклічність вимагається лише від **порядкових** ребер (`determines`,
`blocks`): цикл у них означав би дедлок «спитай A перед B і B перед A». Ребра
`invalidates` і `reinforces` можуть бути взаємними; від них вимагається лише
відсутність самопетлі і наявність цілі.

**Матеріалізований checkout scope.** Рівно 14 checkout-вузлів беруться з
`checkout_readiness` і даних угоди (`np_full_name`/`np_phone`/`np_city`/
`np_office`, `pay_type`, invoice), включно з двома під-умовами `size`.
Окремо B09.1 додає structural-only semantic definitions: вони не мають state
projector, не пишуться в БД і не розширюють `IgClient.stage`.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from management.models import IgFunnelNodeState

logger = logging.getLogger(__name__)

DEFINITION_VERSION = "funnel-node.v1.2"
PROJECTOR_VERSION = "funnel-node-projector.v1"

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
PROJECTION_MODE_FLAG = "IG_FUNNEL_NODE_PROJECTION_MODE"


class NodeClass:
    """Три класи вузлів. Різниця не в тому, «наскільки важливо», а в тому, що
    дозволено робити: BLOCKING не можна пропустити в жодному режимі, QUALITY можна
    пропустити **із записом причини**, CONTEXT ніколи не питається напряму."""

    BLOCKING = "blocking"
    QUALITY = "quality"
    CONTEXT = "context"

    ALL = frozenset({BLOCKING, QUALITY, CONTEXT})


class DependencyKind:
    """Рівно чотири типи залежностей, без умовної логіки.

    Якщо залежність не виражається ними — вона лишається імперативною в тому
    шарі, який знає факт. П'ятий тип «якщо…то» перетворив би граф на другу мову
    програмування без відладчика.
    """

    # A задає множину допустимих значень B (крій визначає розмірну сітку).
    DETERMINES = "determines"
    # Зміна A робить значення B недопустимим (змінили колір — розміру немає).
    INVALIDATES = "invalidates"
    # A не потрібен, але його закриття зменшує ризик поганого результату
    # (кількість і тип оплати не блокують посилання, але роблять суму правильною).
    REINFORCES = "reinforces"
    # Поки A не закритий, B не можна навіть питати (відділення до міста).
    BLOCKS = "blocks"

    ALL = frozenset({DETERMINES, INVALIDATES, REINFORCES, BLOCKS})
    # Ребра, що задають порядок питань; лише вони мусять бути ациклічними.
    ORDERING = frozenset({DETERMINES, BLOCKS})


class IrreversibleAction:
    """Дії, які не можна відкотити перед клієнтом. Тільки вони мають право
    стояти в `blocking_for`.

    Обидві перевірені по коду, а не за відчуттям:
    `checkout_readiness.can_issue_link` (`ig_checkout_readiness.py`) вартує видачу
    посилання, `bot_orders.fulfill_if_ready()` вартує створення замовлення через
    `deal_has_np_data()` + `ig_delivery.has_validated_delivery()`. Бажання зібрати
    маркетинговий контекст сюда не входить.
    """

    PAY_LINK_ISSUE = "pay_link_issue"
    ORDER_CREATE = "order_create"

    ALL = frozenset({PAY_LINK_ISSUE, ORDER_CREATE})


class LifecycleScope:
    """Скільки живе закриття вузла."""

    EPISODE = "episode"
    CLIENT = "client"
    LINE = "line"

    ALL = frozenset({EPISODE, CLIENT, LINE})


class ResetBehavior:
    """Що робиться зі значенням при reset воронки (`ig_funnel_reset`)."""

    CLEAR = "clear"
    KEEP = "keep"
    CONFIRM = "confirm"

    ALL = frozenset({CLEAR, KEEP, CONFIRM})


class RetentionClass:
    """Словник збігається з `IgMemoryFact.retention_class`, щоб два механізми
    пам'яті не описували один і той самий строк життя різними словами."""

    EPISODE = "episode"
    CLIENT = "client"

    ALL = frozenset({EPISODE, CLIENT})


class EvidencePolicy:
    """Чим доводиться закриття. Порожнє закриття = виведення зі стадії."""

    CATALOG_FACT = "catalog_fact"
    CUSTOMER_STATEMENT = "customer_statement"
    DIRECTORY_CONFIRMED = "directory_confirmed"
    PROVIDER_FACT = "provider_fact"
    MESSAGE_OBSERVATION = "message_observation"
    MEDIA_OBSERVATION = "media_observation"
    MANAGER_DECISION = "manager_decision"
    ORDER_FACT = "order_fact"
    CONSENT_FACT = "consent_fact"
    REWARD_FACT = "reward_fact"

    ALL = frozenset({
        CATALOG_FACT, CUSTOMER_STATEMENT, DIRECTORY_CONFIRMED, PROVIDER_FACT,
        MESSAGE_OBSERVATION, MEDIA_OBSERVATION, MANAGER_DECISION, ORDER_FACT,
        CONSENT_FACT, REWARD_FACT,
    })


class ProjectionTarget:
    """Звідки проєктор бере факт. Це поле політики, а не місце виклику:
    воно каже, який шар є джерелом істини для вузла."""

    CHECKOUT_READINESS = "checkout_readiness"
    DEAL_DELIVERY = "deal_delivery"
    DEAL_PAYMENT = "deal_payment"
    # Structural route metadata has no state projector.  It must never create
    # an IgFunnelNodeState merely because the route is possible.
    NONE = "none"

    ALL = frozenset({CHECKOUT_READINESS, DEAL_DELIVERY, DEAL_PAYMENT, NONE})


class SubjectScope:
    """Чий це факт. `line` існує, щоб розмір отримувача подарунка не переписав
    розмір покупця (Э4.4 покладається на це поле)."""

    BUYER = "buyer"
    LINE = "line"

    ALL = frozenset({BUYER, LINE})


class SemanticKind:
    """Role in the structural route catalogue, never observed history."""

    ENTRY = "entry"
    DECISION = "decision"
    SHARED_JOIN = "shared_join"
    CROSS_CUTTING = "cross_cutting"
    TERMINAL = "terminal"

    ALL = frozenset({ENTRY, DECISION, SHARED_JOIN, CROSS_CUTTING, TERMINAL})


class RequirementRole:
    """Whether a fact is required, helpful, or contextual for an action."""

    REQUIRED = "required"
    HELPFUL = "helpful"
    CONTEXT = "context"

    ALL = frozenset({REQUIRED, HELPFUL, CONTEXT})


@dataclass(frozen=True, slots=True)
class NodeDependency:
    """Одне ребро графа. Тільки тип і ціль — жодної умовної логіки."""

    kind: str
    on: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class SemanticTransition:
    """A possible typed link; it is not a recorded customer transition."""

    target_key: str
    outcome: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class StructuralTransition:
    """A source-qualified possible route link returned by the metadata API."""

    source_key: str
    target_key: str
    outcome: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class FunnelNodeDefinition:
    """Канонічне визначення вузла. Сім різниць між обов'язковим і контекстним
    виражені сімома полями політики, а не двома таблицями."""

    key: str
    node_class: str
    ui_label: str
    group: str
    applicable_when: str
    evidence_policy: str
    projection_target: str
    # 1. область життя, 2. клас збереження, 3. поведінка при reset,
    # 4. чий факт, 5. пріоритет питання, 6. клас вузла, 7. необоротні дії.
    lifecycle_scope: str = LifecycleScope.EPISODE
    retention_class: str = RetentionClass.EPISODE
    reset_behavior: str = ResetBehavior.CLEAR
    subject_scope: str = SubjectScope.BUYER
    prompt_priority: int = 0
    parent_key: str = ""
    dependencies: tuple[NodeDependency, ...] = ()
    blocking_for: tuple[str, ...] = ()
    version: str = DEFINITION_VERSION
    # B09.1 metadata. Existing checkout definitions retain empty defaults.
    semantic_kind: str = ""
    route_keys: tuple[str, ...] = ()
    requirement_role: str = ""
    authority_contract: str = ""
    instruction_fragment_key: str = ""
    allowed_outcomes: tuple[str, ...] = ()
    transitions: tuple[SemanticTransition, ...] = ()

    @property
    def level(self) -> int:
        """Рівень вкладеності в термінах плану: 1 — під-воронка (не вузол),
        2 — вузол, 3 — під-умова вузла. Четвертого рівня немає ніде."""
        return 3 if self.parent_key else 2

    @property
    def never_ask(self) -> bool:
        return self.node_class == NodeClass.CONTEXT

    @property
    def skippable(self) -> bool:
        return self.node_class == NodeClass.QUALITY

    @property
    def is_materialized(self) -> bool:
        return self.projection_target != ProjectionTarget.NONE

    def dependencies_of(self, kind: str) -> tuple[str, ...]:
        return tuple(dep.on for dep in self.dependencies if dep.kind == kind)


def _definitions() -> tuple[FunnelNodeDefinition, ...]:
    """Рівно ті вузли, що вже існують. Жодного нового.

    Група — це **мітка**, а не вузол: «доставка» і «оплата» не отримують
    батьківських рядків, бо їх не існує в даних. Третій рівень заведений тільки в
    `size`, де для закриття справді потрібні дві незалежні умови: розмір названий і
    розмір доступний. У `color` умова одна — третього рівня немає.

    Класу CONTEXT тут немає жодного вузла свідомо: усі наявні вузли або блокують
    необоротну дію, або впливають на її якість. Заводити контекстний вузол зараз
    означало б додати новий вузол, що етап прямо забороняє; словник класу існує,
    і валідатор його правила перевіряє.
    """
    determines = DependencyKind.DETERMINES
    invalidates = DependencyKind.INVALIDATES
    reinforces = DependencyKind.REINFORCES
    blocks = DependencyKind.BLOCKS
    pay_link = IrreversibleAction.PAY_LINK_ISSUE
    order_create = IrreversibleAction.ORDER_CREATE
    readiness_target = ProjectionTarget.CHECKOUT_READINESS

    checkout = (
        FunnelNodeDefinition(
            key="product",
            node_class=NodeClass.BLOCKING,
            ui_label="Товар",
            group="configuration",
            applicable_when="always",
            evidence_policy=EvidencePolicy.CATALOG_FACT,
            projection_target=readiness_target,
            prompt_priority=100,
            blocking_for=(pay_link,),
        ),
        FunnelNodeDefinition(
            key="fit",
            node_class=NodeClass.BLOCKING,
            ui_label="Крій",
            group="configuration",
            applicable_when="product_has_fit_axis",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=readiness_target,
            prompt_priority=90,
            blocking_for=(pay_link,),
            dependencies=(
                NodeDependency(determines, "product", "набір кроїв — від товару"),
                NodeDependency(blocks, "product", "крій без товару не існує"),
                NodeDependency(invalidates, "product", "інший товар — інші крої"),
            ),
        ),
        FunnelNodeDefinition(
            key="color",
            node_class=NodeClass.BLOCKING,
            ui_label="Колір",
            group="configuration",
            applicable_when="product_has_multiple_variants",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=readiness_target,
            prompt_priority=85,
            blocking_for=(pay_link,),
            dependencies=(
                NodeDependency(determines, "product", "варіанти кольору — від товару"),
                NodeDependency(blocks, "product", "колір без товару не існує"),
                NodeDependency(invalidates, "product", "інший товар — інші кольори"),
                # Зворотний бік взаємної інвалідації: обраний розмір фільтрує
                # варіанти через `variant_allows_purchase(size=...)`.
                NodeDependency(invalidates, "size", "розмір може відсікти колір"),
            ),
        ),
        FunnelNodeDefinition(
            key="size",
            node_class=NodeClass.BLOCKING,
            ui_label="Розмір",
            group="configuration",
            applicable_when="product_has_size_grid",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=readiness_target,
            prompt_priority=80,
            blocking_for=(pay_link,),
            dependencies=(
                NodeDependency(determines, "fit", "крій визначає сітку розмірів"),
                NodeDependency(determines, "color", "правила розмірів живуть на варіанті"),
                NodeDependency(blocks, "product", "розмір без товару не існує"),
                NodeDependency(invalidates, "product", "інший товар — інша сітка"),
                NodeDependency(invalidates, "fit", "інший крій — інша сітка"),
                NodeDependency(invalidates, "color", "у новому кольорі розміру може не бути"),
            ),
        ),
        FunnelNodeDefinition(
            key="size_named",
            node_class=NodeClass.BLOCKING,
            ui_label="Розмір названий",
            group="configuration",
            applicable_when="product_has_size_grid",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=readiness_target,
            parent_key="size",
            prompt_priority=80,
            dependencies=(
                NodeDependency(blocks, "product", "нема товару — нема сітки"),
            ),
        ),
        FunnelNodeDefinition(
            key="size_available",
            node_class=NodeClass.BLOCKING,
            ui_label="Розмір доступний",
            group="configuration",
            applicable_when="product_has_size_grid",
            evidence_policy=EvidencePolicy.CATALOG_FACT,
            projection_target=readiness_target,
            parent_key="size",
            prompt_priority=0,
            dependencies=(
                NodeDependency(determines, "color", "наявність рахується по варіанту"),
                NodeDependency(determines, "fit", "наявність рахується по крою"),
                NodeDependency(blocks, "size_named", "нема названого розміру — нема чого перевіряти"),
                NodeDependency(invalidates, "color", "зміна кольору робить розмір недоступним"),
                NodeDependency(invalidates, "fit", "зміна крою робить розмір недоступним"),
            ),
        ),
        FunnelNodeDefinition(
            key="option_axes",
            node_class=NodeClass.BLOCKING,
            ui_label="Опції",
            group="configuration",
            applicable_when="product_has_option_axes",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=readiness_target,
            prompt_priority=75,
            blocking_for=(pay_link,),
            dependencies=(
                NodeDependency(determines, "product", "осі опцій — від товару"),
                NodeDependency(blocks, "product", "опції без товару не існують"),
                NodeDependency(invalidates, "product", "інший товар — інші осі"),
            ),
        ),
        FunnelNodeDefinition(
            key="quantity",
            node_class=NodeClass.QUALITY,
            ui_label="Кількість",
            group="configuration",
            applicable_when="always",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=readiness_target,
            prompt_priority=40,
            dependencies=(
                NodeDependency(blocks, "product", "кількість чого саме"),
                # Кількість не блокує посилання (є типове 1), але змінює суму.
                NodeDependency(reinforces, "paylink", "від кількості залежить сума"),
            ),
        ),
        FunnelNodeDefinition(
            key="paylink",
            node_class=NodeClass.BLOCKING,
            ui_label="Посилання на оплату",
            group="payment",
            applicable_when="always",
            evidence_policy=EvidencePolicy.PROVIDER_FACT,
            projection_target=ProjectionTarget.DEAL_PAYMENT,
            prompt_priority=0,
            blocking_for=(order_create,),
            dependencies=(
                NodeDependency(determines, "product", "посилання видається на конфігурацію"),
                NodeDependency(blocks, "product", "нема товару — нема суми"),
                NodeDependency(invalidates, "product", "змінили товар — стара сума не та"),
                NodeDependency(invalidates, "size", "змінили розмір — стара сума не та"),
                NodeDependency(invalidates, "color", "змінили колір — стара сума не та"),
                NodeDependency(invalidates, "option_axes", "змінили опції — стара сума не та"),
                NodeDependency(invalidates, "quantity", "змінили кількість — стара сума не та"),
                NodeDependency(invalidates, "pay_type", "змінили тип оплати — стара сума не та"),
            ),
        ),
        FunnelNodeDefinition(
            key="pay_type",
            node_class=NodeClass.QUALITY,
            ui_label="Тип оплати",
            group="payment",
            applicable_when="deal_exists",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=ProjectionTarget.DEAL_PAYMENT,
            prompt_priority=35,
            dependencies=(
                NodeDependency(blocks, "product", "тип оплати чого саме"),
                NodeDependency(reinforces, "paylink", "від типу оплати залежить сума"),
            ),
        ),
        # Логістику план вимагає питати останньою: це бар'єр перед рішенням.
        # Тому пріоритети тут нижчі за конфігурацію, а не тому, що вона менш важлива.
        FunnelNodeDefinition(
            key="city",
            node_class=NodeClass.BLOCKING,
            ui_label="Місто",
            group="delivery",
            applicable_when="deal_exists",
            evidence_policy=EvidencePolicy.DIRECTORY_CONFIRMED,
            projection_target=ProjectionTarget.DEAL_DELIVERY,
            prompt_priority=30,
            blocking_for=(order_create,),
        ),
        FunnelNodeDefinition(
            key="branch",
            node_class=NodeClass.BLOCKING,
            ui_label="Відділення",
            group="delivery",
            applicable_when="deal_exists",
            evidence_policy=EvidencePolicy.DIRECTORY_CONFIRMED,
            projection_target=ProjectionTarget.DEAL_DELIVERY,
            prompt_priority=25,
            blocking_for=(order_create,),
            dependencies=(
                NodeDependency(determines, "city", "довідник відділень — від міста"),
                NodeDependency(blocks, "city", "відділення без міста не існує"),
                NodeDependency(invalidates, "city", "змінили місто — відділення не те"),
            ),
        ),
        FunnelNodeDefinition(
            key="recipient_name",
            node_class=NodeClass.BLOCKING,
            ui_label="Отримувач",
            group="delivery",
            applicable_when="deal_exists",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=ProjectionTarget.DEAL_DELIVERY,
            # Отримувач може бути не покупцем — саме тому scope лінії, а не покупця.
            subject_scope=SubjectScope.LINE,
            prompt_priority=20,
            blocking_for=(order_create,),
        ),
        FunnelNodeDefinition(
            key="phone",
            node_class=NodeClass.BLOCKING,
            ui_label="Телефон",
            group="delivery",
            applicable_when="deal_exists",
            evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT,
            projection_target=ProjectionTarget.DEAL_DELIVERY,
            subject_scope=SubjectScope.LINE,
            # Телефон переживає reset: він не змінюється від того, що діалог
            # почався заново, і питати його вдруге — та сама «тупість» бота.
            lifecycle_scope=LifecycleScope.CLIENT,
            retention_class=RetentionClass.CLIENT,
            reset_behavior=ResetBehavior.CONFIRM,
            prompt_priority=15,
            blocking_for=(order_create,),
        ),
    )
    return checkout + _semantic_definitions()


def _semantic_definitions() -> tuple[FunnelNodeDefinition, ...]:
    """Static D064 route structure, deliberately disconnected from projection.

    Each definition remains in the canonical registry so bot and map can share
    keys later.  `authority_contract` names the producer that must be linked
    before a route can become active; this catalogue itself proves nothing.
    """
    def route(
        key: str, label: str, kind: str, route_key: str, *,
        authority: str, evidence_policy: str, outcomes: tuple[str, ...] = (),
        transitions: tuple[SemanticTransition, ...] = (),
        instruction: str = "",
    ) -> FunnelNodeDefinition:
        return FunnelNodeDefinition(
            key=key,
            node_class=NodeClass.CONTEXT,
            ui_label=label,
            group="journey",
            applicable_when="producer_linked",
            evidence_policy=evidence_policy,
            projection_target=ProjectionTarget.NONE,
            semantic_kind=kind,
            route_keys=(route_key,),
            requirement_role=RequirementRole.CONTEXT,
            authority_contract=authority,
            instruction_fragment_key=instruction,
            allowed_outcomes=outcomes,
            transitions=transitions,
        )

    to = SemanticTransition
    entry = SemanticKind.ENTRY
    decision = SemanticKind.DECISION
    join = SemanticKind.SHARED_JOIN
    cross = SemanticKind.CROSS_CUTTING
    terminal = SemanticKind.TERMINAL
    return (
        route("inbound", "Вхідне звернення", entry, "inbound",
              authority="owned_inbound_message", evidence_policy=EvidencePolicy.MESSAGE_OBSERVATION, transitions=(
                  to("ad_resolved_product"), to("catalog_discovery"), to("photo_reference"),
                  to("custom_print"), to("dtf_only"), to("prize_candidate"),
                  to("information_question"), to("collaboration"), to("employment"),
                  to("spam_confirmed"), to("post_sale_request"),
              )),
        route("ad_resolved_product", "Відомий товар", entry, "catalog",
              authority="resolved_ad_or_product_reference", evidence_policy=EvidencePolicy.CATALOG_FACT, transitions=(to("configured_line"), to("availability_question", "check_selected_availability"))),
        route("catalog_discovery", "Підбір товару", entry, "catalog",
              authority="customer_need_or_catalog_selection", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("configured_line"), to("availability_question", "check_selected_availability"))),
        route("photo_reference", "Фото-референс", entry, "photo",
              authority="owned_media_understanding", evidence_policy=EvidencePolicy.MEDIA_OBSERVATION, transitions=(
                  to("catalog_discovery", "catalog_match"), to("custom_print", "custom_reference"),
                  to("availability_question", "availability"),
              )),
        route("availability_question", "Доступність", decision, "catalog",
              authority="catalog_availability_fact", evidence_policy=EvidencePolicy.CATALOG_FACT, transitions=(
                  to("configured_line", "stock_rechecked_available"), to("stock_wait", "unavailable_wait"),
                  to("catalog_discovery", "choose_alternative"),
              )),
        route("stock_wait", "Очікування наявності", decision, "catalog",
              authority="owned_variant_stock_subscription", evidence_policy=EvidencePolicy.CATALOG_FACT, transitions=(
                  to("restock_consent", "offer_restock_consent"),
                  to("configured_line", "stock_rechecked_available"),
                  to("catalog_discovery", "choose_alternative"),
              )),
        route("restock_consent", "Дозвіл на сповіщення про наявність", decision, "catalog",
              authority="purpose_scoped_restock_channel_grant", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(
                  to("stock_wait", "restock_consent_granted"),
                  to("stock_wait", "restock_consent_not_granted"),
              )),
        route("custom_print", "Кастом", entry, "custom",
              authority="typed_custom_brief_producer", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("custom_brief"),)),
        route("dtf_only", "DTF-плівка", entry, "dtf",
              authority="typed_dtf_brief_producer", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("custom_brief"),)),
        route("custom_brief", "Бриф", decision, "custom",
              authority="typed_custom_brief_producer", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("mockup_current_acceptance"),)),
        route("mockup_current_acceptance", "Узгодження макета принту", decision, "custom",
              authority="versioned_mockup_acceptance", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("configured_line", "current_mockup_accepted"),)),
        route("prize_candidate", "Призовий випадок", entry, "prize",
              authority="validated_prize_case", evidence_policy=EvidencePolicy.MEDIA_OBSERVATION, transitions=(to("prize_decision", "manager_decision"),)),
        route("prize_decision", "Рішення про приз", decision, "prize",
              authority="authorised_prize_entitlement", evidence_policy=EvidencePolicy.MANAGER_DECISION, transitions=(to("configured_line"), to("custom_brief"))),
        route("information_question", "Інформаційне питання", entry, "information",
              authority="owned_customer_question", evidence_policy=EvidencePolicy.MESSAGE_OBSERVATION, transitions=(to("information_resolved"),)),
        route("information_resolved", "Питання закрито", terminal, "information",
              authority="recorded_answer_outcome", evidence_policy=EvidencePolicy.MESSAGE_OBSERVATION, outcomes=("resolved",)),
        route("collaboration", "Співпраця", entry, "collaboration",
              authority="structured_collaboration_intent", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(
                  to("collaboration_designer"), to("collaboration_partnership"),
                  to("collaboration_dropship"), to("collaboration_wholesale_store"),
                  to("collaboration_creator"), to("collaboration_other"),
              )),
        route("collaboration_designer", "Дизайнер", decision, "collaboration",
              authority="published_collaboration_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("business_decision"),)),
        route("collaboration_partnership", "Партнерство", decision, "collaboration",
              authority="published_collaboration_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("business_decision"),)),
        route("collaboration_dropship", "Dropship", decision, "collaboration",
              authority="published_collaboration_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("business_decision"),)),
        route("collaboration_wholesale_store", "Опт або магазин", decision, "collaboration",
              authority="published_collaboration_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("business_decision"),)),
        route("collaboration_creator", "Creator", decision, "collaboration",
              authority="published_collaboration_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("business_decision"),)),
        route("collaboration_other", "Інша співпраця", decision, "collaboration",
              authority="published_collaboration_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("business_decision"),)),
        route("business_decision", "Ділове рішення", terminal, "collaboration",
              authority="authorised_business_decision", evidence_policy=EvidencePolicy.MANAGER_DECISION, outcomes=("resolved", "declined"),
              transitions=(to("new_purchase_interest", "new_customer_interest"),)),
        route("employment", "Робота в команді", entry, "employment",
              authority="published_employment_policy", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("employment_response"),)),
        route("employment_response", "Відповідь щодо роботи", terminal, "employment",
              authority="authorised_employment_decision", evidence_policy=EvidencePolicy.MANAGER_DECISION, outcomes=("resolved", "unknown_policy")),
        route("spam_confirmed", "Підтверджений спам", terminal, "spam",
              authority="deterministic_spam_or_authorised_decision", evidence_policy=EvidencePolicy.MANAGER_DECISION, outcomes=("blocked", "corrected")),
        route("configured_line", "Підтверджений склад", join, "commerce",
              authority="canonical_line_configuration", evidence_policy=EvidencePolicy.CATALOG_FACT, transitions=(to("quoted_offer"),)),
        route("quoted_offer", "Актуальна пропозиція", join, "commerce",
              authority="versioned_offer_or_quote", evidence_policy=EvidencePolicy.MANAGER_DECISION, transitions=(
                  to("awaiting_payment", "payment_required"), to("objection_case"), to("configured_line", "configuration_correction"),
                  to("settlement", "verified_entitlement_covers_total"),
              )),
        route("awaiting_payment", "Очікування оплати", decision, "payment",
              authority="current_invoice_or_payment_attempt", evidence_policy=EvidencePolicy.PROVIDER_FACT, transitions=(
                  to("settlement", "confirmed_coverage"), to("payment_help", "eligible_follow_up"),
                  to("quoted_offer", "offer_correction"), to("configured_line", "configuration_correction"),
              )),
        route("payment_help", "Допомога з оплатою", decision, "payment",
              authority="current_payment_follow_up_and_permission", evidence_policy=EvidencePolicy.PROVIDER_FACT, transitions=(
                  to("awaiting_payment", "wait_for_attempt"), to("quoted_offer", "offer_correction"),
                  to("configured_line", "configuration_correction"), to("objection_case", "payment_objection"),
              )),
        route("settlement", "Розрахунок", join, "commerce",
              authority="payment_or_entitlement_truth", evidence_policy=EvidencePolicy.PROVIDER_FACT, transitions=(
                  to("fulfillment"), to("objection_case"), to("quoted_offer", "settlement_correction"),
                  to("configured_line", "configuration_correction"), to("channel_consent", "eligible_opt_in"),
              )),
        route("fulfillment", "Виконання", join, "commerce",
              authority="bound_order_and_shipment_truth", evidence_policy=EvidencePolicy.ORDER_FACT, transitions=(to("post_sale_case"), to("channel_grant_checked", "permission_check"), to("repeat_interest"), to("channel_consent", "eligible_opt_in"))),
        route("objection_case", "Заперечення", cross, "objection",
              authority="episode_line_bound_objection_case", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("configured_line", "new_selection"), to("quoted_offer", "amended_offer"), to("awaiting_payment", "new_attempt"))),
        route("channel_consent", "Згода на канал", cross, "consent",
              authority="purpose_scoped_consent_and_permission", evidence_policy=EvidencePolicy.CONSENT_FACT, transitions=(to("channel_grant_checked", "consent_recorded"),)),
        route("channel_grant_checked", "Перевірка дозволу каналу", cross, "consent",
              authority="current_server_permission_profile", evidence_policy=EvidencePolicy.PROVIDER_FACT, transitions=(to("post_purchase_contact_offer", "send_capable"),)),
        route("post_purchase_contact_offer", "Пропозиція після покупки", decision, "ugc",
              authority="purpose_scoped_offer_delivery_receipt", evidence_policy=EvidencePolicy.PROVIDER_FACT, transitions=(to("ugc_assessment", "material_received"),)),
        route("post_sale_request", "Сервісний запит", entry, "post_sale",
              authority="bound_order_or_service_request", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("post_sale_case"),)),
        route("post_sale_case", "Сервісний випадок", cross, "post_sale",
              authority="post_sale_case_and_shipment_truth", evidence_policy=EvidencePolicy.ORDER_FACT, outcomes=("resolved", "rejected", "cancelled"),
              transitions=(to("configured_line", "new_need"),)),
        route("ugc_assessment", "Перевірка UGC", cross, "ugc",
              authority="ugc_evidence_assessment", evidence_policy=EvidencePolicy.MEDIA_OBSERVATION, transitions=(to("reward_entitlement", "authorised_reward_grant"),)),
        route("reward_entitlement", "Право на нагороду", cross, "reward",
              authority="authorised_reward_entitlement", evidence_policy=EvidencePolicy.REWARD_FACT, transitions=(to("reward_delivery"),)),
        route("reward_delivery", "Видача нагороди", cross, "reward",
              authority="reward_delivery_receipt", evidence_policy=EvidencePolicy.PROVIDER_FACT, transitions=(to("reward_use"),)),
        route("reward_use", "Використання нагороди", cross, "reward",
              authority="reward_redemption_truth", evidence_policy=EvidencePolicy.REWARD_FACT, transitions=(to("new_purchase_interest"),)),
        route("repeat_interest", "Новий інтерес", cross, "repeat",
              authority="explicit_repeat_evidence", evidence_policy=EvidencePolicy.CUSTOMER_STATEMENT, transitions=(to("new_purchase_interest"),)),
        route("new_purchase_interest", "Нова покупка", join, "repeat",
              authority="new_commercial_episode", evidence_policy=EvidencePolicy.ORDER_FACT, transitions=(
                  to("catalog_discovery", "new_selection"), to("configured_line", "current_configuration_confirmed"),
              )),
    )


REGISTRY: dict[str, FunnelNodeDefinition] = {
    definition.key: definition for definition in _definitions()
}
NODE_KEYS: tuple[str, ...] = tuple(REGISTRY)
MATERIALIZED_NODE_KEYS: tuple[str, ...] = tuple(
    key for key, definition in REGISTRY.items() if definition.is_materialized
)
SEMANTIC_NODE_KEYS: tuple[str, ...] = tuple(
    key for key, definition in REGISTRY.items() if not definition.is_materialized
)


class RegistryError(ValueError):
    """Граф не пройшов статичну перевірку: цикл, висяче посилання або порушене
    правило класу вузлів."""


def validate_registry(registry: dict | None = None) -> tuple[str, ...]:
    """Статична перевірка графа. Повертає список проблем (порожній — все добре).

    Перевіряються **порядкові** цикли, висячі посилання, глибина (немає
    четвертого рівня) і правила класів. Ациклічність `invalidates`/`reinforces`
    свідомо не вимагається: колір відсікає розміри, а розмір відсікає кольори — це
    правда каталогу, і заборона взаємності змусила б її приховати.
    """
    nodes = REGISTRY if registry is None else registry
    problems: list[str] = []

    for key, definition in nodes.items():
        if key != definition.key:
            problems.append(f"{key}: ключ реєстру не збігається з визначенням")
        if definition.node_class not in NodeClass.ALL:
            problems.append(f"{key}: невідомий клас {definition.node_class!r}")
        if definition.lifecycle_scope not in LifecycleScope.ALL:
            problems.append(f"{key}: невідома область життя {definition.lifecycle_scope!r}")
        if definition.retention_class not in RetentionClass.ALL:
            problems.append(f"{key}: невідомий клас збереження {definition.retention_class!r}")
        if definition.reset_behavior not in ResetBehavior.ALL:
            problems.append(f"{key}: невідома поведінка при reset {definition.reset_behavior!r}")
        if definition.subject_scope not in SubjectScope.ALL:
            problems.append(f"{key}: невідомий суб'єкт {definition.subject_scope!r}")
        if definition.evidence_policy not in EvidencePolicy.ALL:
            problems.append(f"{key}: невідома політика доказу {definition.evidence_policy!r}")
        if definition.projection_target not in ProjectionTarget.ALL:
            problems.append(f"{key}: невідома проєкція {definition.projection_target!r}")
        if definition.semantic_kind and definition.semantic_kind not in SemanticKind.ALL:
            problems.append(f"{key}: невідомий semantic kind {definition.semantic_kind!r}")
        if definition.requirement_role and definition.requirement_role not in RequirementRole.ALL:
            problems.append(f"{key}: невідома semantic requirement role {definition.requirement_role!r}")
        if not definition.is_materialized:
            if definition.node_class != NodeClass.CONTEXT:
                problems.append(f"{key}: semantic-only вузол має бути CONTEXT")
            if not definition.semantic_kind or not definition.route_keys:
                problems.append(f"{key}: semantic-only вузол не має typed metadata")
            if not str(definition.authority_contract or "").strip():
                problems.append(f"{key}: semantic-only вузол не має authority contract")
            if definition.prompt_priority:
                problems.append(f"{key}: semantic-only вузол не може мати prompt priority")
            if definition.dependencies:
                problems.append(f"{key}: semantic-only вузол не може мати checkout dependencies")
            if definition.blocking_for:
                problems.append(f"{key}: semantic-only вузол не може блокувати дію")
        elif definition.semantic_kind or definition.route_keys or definition.transitions:
            problems.append(f"{key}: materialized вузол не може мати route metadata")

        for action in definition.blocking_for:
            if action not in IrreversibleAction.ALL:
                problems.append(
                    f"{key}: blocking_for={action!r} не є необоротною дією"
                )
        if definition.node_class == NodeClass.BLOCKING and not definition.blocking_for:
            parent = nodes.get(definition.parent_key)
            if parent is None or parent.node_class != NodeClass.BLOCKING:
                problems.append(
                    f"{key}: BLOCKING без blocking_for і без блокуючого батька"
                )
        if definition.node_class != NodeClass.BLOCKING and definition.blocking_for:
            problems.append(
                f"{key}: {definition.node_class} не має права блокувати необоротну дію"
            )
        if definition.never_ask and definition.prompt_priority:
            problems.append(f"{key}: CONTEXT не питається напряму, пріоритет мусить бути 0")

        if definition.parent_key:
            parent = nodes.get(definition.parent_key)
            if parent is None:
                problems.append(f"{key}: висяче посилання на батька {definition.parent_key!r}")
            elif parent.parent_key:
                problems.append(f"{key}: четвертий рівень вкладеності заборонений")
            elif definition.parent_key == key:
                problems.append(f"{key}: сам собі батько")

        for dependency in definition.dependencies:
            if dependency.kind not in DependencyKind.ALL:
                problems.append(f"{key}: невідомий тип залежності {dependency.kind!r}")
            if dependency.on not in nodes:
                problems.append(f"{key}: висяче посилання на {dependency.on!r}")
            elif dependency.on == key:
                problems.append(f"{key}: залежність від самого себе")
            elif definition.is_materialized and not nodes[dependency.on].is_materialized:
                problems.append(f"{key}: projected вузол залежить від semantic-only {dependency.on!r}")

        for transition in definition.transitions:
            if not isinstance(transition, SemanticTransition):
                problems.append(f"{key}: некоректний semantic transition")
                continue
            if transition.target_key not in nodes:
                problems.append(f"{key}: semantic transition на невідомий вузол {transition.target_key!r}")
            elif nodes[transition.target_key].is_materialized:
                problems.append(f"{key}: semantic transition не може вести до checkout field {transition.target_key!r}")

    problems.extend(_ordering_cycles(nodes))
    return tuple(problems)


def _ordering_cycles(nodes: dict) -> list[str]:
    """Цикли лише в ребрах, що задають порядок питань: цикл там — це дедлок."""
    edges: dict[str, tuple[str, ...]] = {}
    for key, definition in nodes.items():
        targets = []
        for dependency in definition.dependencies:
            if dependency.kind not in DependencyKind.ORDERING:
                continue
            if dependency.on in nodes and dependency.on != key:
                targets.append(dependency.on)
        if definition.parent_key in nodes and definition.parent_key != key:
            targets.append(definition.parent_key)
        edges[key] = tuple(dict.fromkeys(targets))

    problems: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(key: str, trail: tuple[str, ...]) -> None:
        if key in done:
            return
        if key in visiting:
            cycle = " → ".join(trail + (key,))
            problems.append(f"цикл у порядкових ребрах: {cycle}")
            return
        visiting.add(key)
        for target in edges.get(key, ()):
            walk(target, trail + (key,))
        visiting.discard(key)
        done.add(key)

    for key in edges:
        walk(key, ())
    return problems


def assert_registry_valid(registry: dict | None = None) -> None:
    problems = validate_registry(registry)
    if problems:
        raise RegistryError("; ".join(problems))


# Перевірка на імпорті, а не тільки в тестах: реєстр із циклом дає тихо
# неправильний порядок питань у кожного споживача, і це найдорожчий вид поломки —
# той, що виглядає працюючим.
assert_registry_valid()


# Обхід графа кешується для стандартного реєстру: він однаковий на кожному ході.
_DEFAULT_ASK_ORDER: tuple[str, ...] | None = None


def definition_for(key: str) -> FunnelNodeDefinition:
    try:
        return REGISTRY[key]
    except KeyError:
        raise RegistryError(f"невідомий вузол воронки: {key!r}") from None


def semantic_definitions() -> tuple[FunnelNodeDefinition, ...]:
    """Return static D064 definitions, never per-client state or history."""
    return tuple(REGISTRY[key] for key in SEMANTIC_NODE_KEYS)


def semantic_definition_for(key: str) -> FunnelNodeDefinition:
    """Return one structural definition and reject checkout-state keys."""
    definition = definition_for(key)
    if definition.is_materialized:
        raise RegistryError(f"{key!r} не є semantic-only вузлом")
    return definition


def structural_transitions(route_keys: tuple[str, ...] = ()) -> tuple[StructuralTransition, ...]:
    """Return possible links only; callers must not render these as history."""
    selected = frozenset(route_keys)
    result = []
    for definition in semantic_definitions():
        if selected and not selected.intersection(definition.route_keys):
            continue
        result.extend(
            StructuralTransition(
                source_key=definition.key,
                target_key=transition.target_key,
                outcome=transition.outcome,
                note=transition.note,
            )
            for transition in definition.transitions
        )
    return tuple(result)


def ask_order(registry: dict | None = None) -> tuple[str, ...]:
    """Порядок питань з обходу графа, а не з константи в коді.

    Спершу те, що звужує решту (`determines`/`blocks`), далі пріоритет політики.
    Імперативні перевірки «якщо немає крою — питай крій, інакше якщо немає
    розміру…» замінюються цим обходом. Для стандартного реєстру результат
    кешується: обхід рахується на кожному ході, а реєстр незмінний.
    """
    global _DEFAULT_ASK_ORDER
    if registry is None and _DEFAULT_ASK_ORDER is not None:
        return _DEFAULT_ASK_ORDER
    nodes = REGISTRY if registry is None else registry
    ordered: list[str] = []
    placed: set[str] = set()

    def rank(key: str) -> tuple[int, str]:
        definition = nodes[key]
        return (-definition.prompt_priority, definition.key)

    def place(key: str, trail: frozenset[str]) -> None:
        if key in placed or key in trail:
            return
        definition = nodes[key]
        prerequisites = sorted(
            {
                dependency.on
                for dependency in definition.dependencies
                if dependency.kind in DependencyKind.ORDERING and dependency.on in nodes
            },
            key=rank,
        )
        for prerequisite in prerequisites:
            place(prerequisite, trail | {key})
        if key not in placed:
            placed.add(key)
            ordered.append(key)

    for key in sorted(nodes, key=rank):
        place(key, frozenset())
    result = tuple(
        key
        for key in ordered
        if nodes[key].prompt_priority > 0 and not nodes[key].never_ask
    )
    if registry is None:
        _DEFAULT_ASK_ORDER = result
    return result


def validate_skip(key: str, reason_code: str) -> None:
    """Пропуск дозволений лише класу QUALITY і лише з причиною."""
    definition = definition_for(key)
    if not definition.skippable:
        raise RegistryError(
            f"{key}: {definition.node_class} не можна пропустити ні в якому режимі"
        )
    if not str(reason_code or "").strip():
        raise RegistryError(f"{key}: пропуск без причини не записується")


_STATUS = IgFunnelNodeState.Status
_CLOSED = IgFunnelNodeState.closed_statuses()


@dataclass(frozen=True, slots=True)
class NodeProjection:
    """Стан одного вузла, порахований із фактів. `stage` не читається ніде."""

    definition: FunnelNodeDefinition
    status: str
    reason_code: str = ""
    reason_detail: str = ""
    typed_value: dict = field(default_factory=dict)
    evidence_message_ids: tuple[int, ...] = ()
    closure_method: str = ""
    invalidated_by: str = ""

    @property
    def key(self) -> str:
        return self.definition.key

    @property
    def is_closed(self) -> bool:
        return self.status in _CLOSED


@dataclass(frozen=True, slots=True)
class FunnelProjection:
    """Проєкція вузлів поруч із авторитетним `checkout_readiness`.

    `payable_ready` береться **тільки** з `can_issue_link`. `authority_agrees`
    існує, щоб розходження було видно як факт, а не тихо перекрило авторитет:
    відкат етапу — це перемикання джерела флагом, а не суперечка двох правд.
    """

    nodes: tuple[NodeProjection, ...]
    payable_ready: bool
    authority_agrees: bool
    projector_version: str = PROJECTOR_VERSION

    def by_key(self) -> dict[str, NodeProjection]:
        return {node.key: node for node in self.nodes}

    def blocking_gaps(self, action: str) -> tuple[str, ...]:
        return _blocking_gaps(self.by_key(), action)

    def next_questions(self) -> tuple[str, ...]:
        by_key = self.by_key()
        return tuple(
            key
            for key in ask_order()
            if key in by_key
            and not by_key[key].is_closed
            and not _blocked_by_unmet(by_key, REGISTRY[key])
        )

    def open_keys(self) -> tuple[str, ...]:
        return tuple(node.key for node in self.nodes if not node.is_closed)


def _blocked_by_unmet(by_key: dict, definition: FunnelNodeDefinition) -> bool:
    for target in definition.dependencies_of(DependencyKind.BLOCKS):
        blocker = by_key.get(target)
        if blocker is not None and not blocker.is_closed:
            return True
    return False


def _blocking_gaps(by_key: dict, action: str) -> tuple[str, ...]:
    """Незакриті вузли, що стоять між нами і необоротною дією.

    Батько згортається в конкретну під-умову: коли клієнт назвав розмір, якого
    немає, гальмує `size_available`, і сказати про це можна словами. Раніше і те,
    і те виглядало як `missing: ["size"]`.
    """
    if action not in IrreversibleAction.ALL:
        raise RegistryError(f"невідома необоротна дія: {action!r}")
    order = {key: index for index, key in enumerate(ask_order())}
    gaps: list[str] = []
    for key, definition in REGISTRY.items():
        if action not in definition.blocking_for:
            continue
        projection = by_key.get(key)
        if projection is None or projection.is_closed:
            continue
        children = [
            child_key
            for child_key, child in REGISTRY.items()
            if child.parent_key == key
        ]
        unmet = [
            child_key
            for child_key in children
            if child_key in by_key
            and not by_key[child_key].is_closed
            and not _blocked_by_unmet(by_key, REGISTRY[child_key])
        ]
        gaps.extend(unmet or [key])
    return tuple(sorted(dict.fromkeys(gaps), key=lambda key: order.get(key, 10**6)))


class Reason:
    """Причини статусів. Вони існують, бо `not_applicable` мусить означати
    політику, а `open` — незнання, і в UI це різні кольори (Э4.3)."""

    NOT_SELECTED = "not_selected"
    AWAITING_PRODUCT = "awaiting_product"
    CATALOG_UNPUBLISHED = "catalog_unpublished"
    NO_FIT_AXIS = "no_fit_axis"
    NO_SIZE_GRID = "no_size_grid"
    NO_COLOR_AXIS = "no_color_axis"
    SINGLE_VARIANT = "single_variant"
    NO_OPTION_AXES = "no_option_axes"
    OPTION_CONTEXT_UNAVAILABLE = "option_context_unavailable"
    AXES_INCOMPLETE = "axes_incomplete"
    REQUESTED_SIZE_UNAVAILABLE = "requested_size_unavailable"
    AWAITING_SIZE_NAMED = "awaiting_size_named"
    ASSUMED_DEFAULT = "assumed_default"
    NO_DEAL = "no_deal"
    NOT_PROVIDED = "not_provided"
    AWAITING_DIRECTORY = "awaiting_directory_confirmation"
    AWAITING_CITY = "awaiting_city"
    LINK_NOT_ISSUED = "link_not_issued"
    LINK_EXPIRED = "link_expired"
    LINK_TTL_UNKNOWN = "link_ttl_unknown"
    LINK_LIVE = "link_live"
    LINK_PAID = "link_paid"


class ClosureMethod:
    """Чим доведено закриття. Порожньо тільки для `open`."""

    CHECKOUT_READINESS = "checkout_readiness"
    CATALOG_POLICY = "catalog_policy"
    DEAL_NP_DATA = "deal_np_data"
    DEAL_PAY_TYPE = "deal_pay_type"
    NP_DIRECTORY = "np_directory"
    INVOICE_STATE = "invoice_link_state"


def _node(
    key: str,
    status: str,
    *,
    reason_code: str = "",
    reason_detail: str = "",
    typed_value: dict | None = None,
    closure_method: str = "",
    invalidated_by: str = "",
    evidence_message_ids: tuple[int, ...] = (),
) -> NodeProjection:
    return NodeProjection(
        definition=definition_for(key),
        status=status,
        reason_code=reason_code,
        reason_detail=reason_detail,
        typed_value=dict(typed_value or {}),
        evidence_message_ids=tuple(evidence_message_ids),
        closure_method=closure_method,
        invalidated_by=invalidated_by,
    )


def _awaiting_product(key: str) -> NodeProjection:
    """Поки товар невідомий, решта конфігурації — `open`, а не `not_applicable`.

    Це рівно та помилка, через яку жовтий статус зливався з незнанням: без товару
    ми не знаємо, чи буде питання про крій, і сказати «не застосовується» означало
    б видати незнання за політику.
    """
    return _node(key, _STATUS.OPEN, reason_code=Reason.AWAITING_PRODUCT)


def _project_product(readiness: dict) -> NodeProjection:
    product = readiness.get("product") or {}
    if readiness.get("has_product"):
        return _node(
            "product",
            _STATUS.COMPLETE,
            typed_value={"product_id": product.get("id"), "title": product.get("title") or ""},
            closure_method=ClosureMethod.CHECKOUT_READINESS,
        )
    if product.get("id"):
        # Товар був обраний і зник із продажу. Це не «не обрано»: клієнту треба
        # сказати правду і запропонувати інший, а не питати «що вас цікавить».
        return _node(
            "product",
            _STATUS.INVALIDATED,
            reason_code=Reason.CATALOG_UNPUBLISHED,
            reason_detail=f"product_id={product.get('id')}",
            typed_value={"product_id": product.get("id")},
            closure_method=ClosureMethod.CHECKOUT_READINESS,
        )
    return _node("product", _STATUS.OPEN, reason_code=Reason.NOT_SELECTED)


def _project_fit(readiness: dict) -> NodeProjection:
    fit = readiness.get("fit") or {}
    if not fit.get("required"):
        return _node(
            "fit",
            _STATUS.NOT_APPLICABLE,
            reason_code=Reason.NO_FIT_AXIS,
            closure_method=ClosureMethod.CATALOG_POLICY,
        )
    if fit.get("selected"):
        return _node(
            "fit",
            _STATUS.COMPLETE,
            typed_value={"fit": fit.get("selected")},
            closure_method=ClosureMethod.CHECKOUT_READINESS,
        )
    return _node("fit", _STATUS.OPEN, reason_code=Reason.NOT_SELECTED)


def _project_color(readiness: dict) -> NodeProjection:
    color = readiness.get("color") or {}
    options = color.get("options") or []
    variant_id = color.get("selected_variant_id")
    if not options:
        # `checkout_readiness` не розрізняє «у товару немає осі кольору» і «всі
        # варіанти відфільтровані»: `_color_rows` в обох випадках віддає []. Вона
        # сама трактує це як «питання немає» (`color` не потрапляє в `missing`),
        # і вигадувати тут різницю означало б угадувати.
        return _node(
            "color",
            _STATUS.NOT_APPLICABLE,
            reason_code=Reason.NO_COLOR_AXIS,
            closure_method=ClosureMethod.CATALOG_POLICY,
        )
    if not color.get("required"):
        return _node(
            "color",
            _STATUS.NOT_APPLICABLE,
            reason_code=Reason.SINGLE_VARIANT,
            typed_value={"variant_id": variant_id},
            closure_method=ClosureMethod.CATALOG_POLICY,
        )
    if variant_id:
        return _node(
            "color",
            _STATUS.COMPLETE,
            typed_value={"variant_id": variant_id, "name": color.get("selected") or ""},
            closure_method=ClosureMethod.CHECKOUT_READINESS,
        )
    return _node("color", _STATUS.OPEN, reason_code=Reason.NOT_SELECTED)


def _project_size(readiness: dict) -> list[NodeProjection]:
    """Розмір і дві його незалежні під-умови.

    Третій рівень тут заслужений: «розмір названий» і «розмір доступний» —
    справді різні умови з різними діями. Клієнт назвав L, у цьому кольорі L немає:
    перше закрито, друге ні, і бот мусить сказати саме це.
    """
    size = readiness.get("size") or {}
    unavailable = str(size.get("requested_unavailable") or "")
    selected = str(size.get("selected") or "")
    if not size.get("required"):
        return [
            _node(
                key,
                _STATUS.NOT_APPLICABLE,
                reason_code=Reason.NO_SIZE_GRID,
                closure_method=ClosureMethod.CATALOG_POLICY,
            )
            for key in ("size", "size_named", "size_available")
        ]

    available = list(size.get("available") or [])
    if selected:
        value = {"size": selected}
        return [
            _node("size", _STATUS.COMPLETE, typed_value=value,
                  closure_method=ClosureMethod.CHECKOUT_READINESS),
            _node("size_named", _STATUS.COMPLETE, typed_value=value,
                  closure_method=ClosureMethod.CHECKOUT_READINESS),
            _node("size_available", _STATUS.COMPLETE, typed_value=value,
                  closure_method=ClosureMethod.CHECKOUT_READINESS),
        ]
    if unavailable:
        # Причина вже є в даних (`requested_unavailable`), а от **який вузол**
        # зробив розмір недоступним — ні: `checkout_readiness` віддає підсумок,
        # а не подію зміни. Атрибуцію дає Э4.2, і вигадувати її тут не можна.
        value = {"requested_size": unavailable, "available": available}
        return [
            _node("size", _STATUS.INVALIDATED, reason_code=Reason.REQUESTED_SIZE_UNAVAILABLE,
                  reason_detail=unavailable, typed_value=value,
                  closure_method=ClosureMethod.CHECKOUT_READINESS),
            _node("size_named", _STATUS.COMPLETE, typed_value={"size": unavailable},
                  closure_method=ClosureMethod.CHECKOUT_READINESS),
            _node("size_available", _STATUS.INVALIDATED,
                  reason_code=Reason.REQUESTED_SIZE_UNAVAILABLE, reason_detail=unavailable,
                  typed_value=value, closure_method=ClosureMethod.CHECKOUT_READINESS),
        ]
    return [
        _node("size", _STATUS.OPEN, reason_code=Reason.NOT_SELECTED,
              typed_value={"available": available}),
        _node("size_named", _STATUS.OPEN, reason_code=Reason.NOT_SELECTED),
        _node("size_available", _STATUS.OPEN, reason_code=Reason.AWAITING_SIZE_NAMED),
    ]


def _project_option_axes(readiness: dict) -> NodeProjection:
    """Порядок перевірок тут важливіший за їхній зміст.

    `missing` дивиться **раніше** за `required`: `checkout_readiness` додає
    `option:<code>` навіть коли осей уже немає (застаріле значення в
    `sales_context` після зміни товару). Перевірка `required` першою дала б
    `not_applicable` там, де посилання насправді заблоковане, — тобто проєкція
    почала б суперечити авторитету.
    """
    options = readiness.get("options") or {}
    axes = options.get("axes") or []
    missing = [str(code) for code in options.get("missing") or []]
    chosen = {
        str(axis.get("code")): str(axis.get("selected") or "")
        for axis in axes
        if str(axis.get("selected") or "")
    }
    if options.get("error"):
        # Збій довідника опцій — це `open`, а не «не застосовується»: ми не знаємо
        # відповіді, і `checkout_readiness` теж блокує посилання в цьому разі.
        return _node(
            "option_axes",
            _STATUS.OPEN,
            reason_code=Reason.OPTION_CONTEXT_UNAVAILABLE,
        )
    if missing:
        value = {"selected": chosen, "missing": missing}
        if chosen:
            # Частина осей закрита — це `partial`, і саме тому статус існує: інакше
            # «дві з трьох опцій обрані» виглядало б як «нічого не обрано».
            return _node(
                "option_axes",
                _STATUS.PARTIAL,
                reason_code=Reason.AXES_INCOMPLETE,
                reason_detail=",".join(missing)[:200],
                typed_value=value,
                closure_method=ClosureMethod.CHECKOUT_READINESS,
            )
        return _node(
            "option_axes",
            _STATUS.OPEN,
            reason_code=Reason.NOT_SELECTED,
            reason_detail=",".join(missing)[:200],
            typed_value=value,
        )
    if not options.get("required"):
        return _node(
            "option_axes",
            _STATUS.NOT_APPLICABLE,
            reason_code=Reason.NO_OPTION_AXES,
            closure_method=ClosureMethod.CATALOG_POLICY,
        )
    return _node(
        "option_axes",
        _STATUS.COMPLETE,
        typed_value={"selected": chosen},
        closure_method=ClosureMethod.CHECKOUT_READINESS,
    )


def _project_quantity(readiness: dict) -> NodeProjection:
    """Кількість: `partial`, а не `complete`, коли значення — типове.

    `current_qty` має default 1, тому «клієнт сказав один» і «ми нічого не знаємо»
    в даних не відрізняються. `complete` тут був би тим самим виведенням, від
    якого етап відмовляється; `open` змусив би follow-up питати кількість на
    кожній угоді. `partial` — єдине чесне: значення робоче, доказу немає.
    """
    try:
        quantity = max(1, int(readiness.get("quantity") or 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity > 1:
        return _node(
            "quantity",
            _STATUS.COMPLETE,
            typed_value={"quantity": quantity},
            closure_method=ClosureMethod.CHECKOUT_READINESS,
        )
    return _node(
        "quantity",
        _STATUS.PARTIAL,
        reason_code=Reason.ASSUMED_DEFAULT,
        typed_value={"quantity": quantity, "assumed": True},
        closure_method=ClosureMethod.CHECKOUT_READINESS,
    )


def _project_delivery(deal) -> list[NodeProjection]:
    """Місто, відділення, отримувач, телефон — з тих самих полів, які вартують
    `bot_orders.fulfill_if_ready()`.

    У `typed_value` не потрапляє ні номер, ні ім'я: вузол доводить **наявність**
    і підтвердження довідником, а не зберігає копію персональних даних. Копія
    жила б у ще одному місці, і її довелось би видаляти при erasure окремо.
    """
    keys = ("city", "branch", "recipient_name", "phone")
    if deal is None:
        return [
            _node(key, _STATUS.OPEN, reason_code=Reason.NO_DEAL) for key in keys
        ]

    from management.services.ig_delivery import delivery_refs_present, has_validated_delivery

    validated = has_validated_delivery(deal)
    refs_present = delivery_refs_present(deal)
    city_display = str(getattr(deal, "np_city", "") or "").strip()
    office_display = str(getattr(deal, "np_office", "") or "").strip()
    name_present = bool(str(getattr(deal, "np_full_name", "") or "").strip())
    phone_present = bool(str(getattr(deal, "np_phone", "") or "").strip())

    def directory_node(key: str, display: str, extra: dict) -> NodeProjection:
        if not display:
            return _node(key, _STATUS.OPEN, reason_code=Reason.NOT_PROVIDED)
        if validated:
            return _node(
                key,
                _STATUS.COMPLETE,
                typed_value=extra,
                closure_method=ClosureMethod.NP_DIRECTORY,
            )
        # Значення для розмови є, а Ref для відправки — ні. Це `partial`, і
        # `fulfill_if_ready` тут теж не пропускає: `has_validated_delivery`.
        return _node(
            key,
            _STATUS.PARTIAL,
            reason_code=Reason.AWAITING_DIRECTORY,
            typed_value=dict(extra, refs_present=refs_present),
            closure_method=ClosureMethod.DEAL_NP_DATA,
        )

    city = directory_node("city", city_display, {"city": city_display})
    if not city_display:
        branch = _node("branch", _STATUS.OPEN, reason_code=Reason.AWAITING_CITY)
    else:
        branch = directory_node("branch", office_display, {"office": office_display})
    return [
        city,
        branch,
        _node(
            "recipient_name",
            _STATUS.COMPLETE if name_present else _STATUS.OPEN,
            reason_code="" if name_present else Reason.NOT_PROVIDED,
            typed_value={"present": True} if name_present else {},
            closure_method=ClosureMethod.DEAL_NP_DATA if name_present else "",
        ),
        _node(
            "phone",
            _STATUS.COMPLETE if phone_present else _STATUS.OPEN,
            reason_code="" if phone_present else Reason.NOT_PROVIDED,
            typed_value={"present": True} if phone_present else {},
            closure_method=ClosureMethod.DEAL_NP_DATA if phone_present else "",
        ),
    ]


def _project_payment(readiness: dict, deal) -> list[NodeProjection]:
    link = readiness.get("link") or {}
    status = str(link.get("status") or "none")
    if status == "paid":
        paylink = _node("paylink", _STATUS.COMPLETE, reason_code=Reason.LINK_PAID,
                        closure_method=ClosureMethod.INVOICE_STATE)
    elif status == "live":
        paylink = _node("paylink", _STATUS.COMPLETE, reason_code=Reason.LINK_LIVE,
                        closure_method=ClosureMethod.INVOICE_STATE)
    elif status == "expired":
        paylink = _node("paylink", _STATUS.INVALIDATED, reason_code=Reason.LINK_EXPIRED,
                        closure_method=ClosureMethod.INVOICE_STATE)
    elif status == "unknown":
        # `invoice_link_state` навмисно віддає `unknown` для посилань, виданих до
        # появи TTL. Записати це як `complete` означало б сказати клієнту
        # «посилання ще активне», не перевіривши нічого — рівно старий дефект.
        paylink = _node("paylink", _STATUS.PARTIAL, reason_code=Reason.LINK_TTL_UNKNOWN,
                        closure_method=ClosureMethod.INVOICE_STATE)
    else:
        paylink = _node("paylink", _STATUS.OPEN, reason_code=Reason.LINK_NOT_ISSUED)

    if deal is None:
        return [paylink, _node("pay_type", _STATUS.OPEN, reason_code=Reason.NO_DEAL)]

    pay_type = str(getattr(deal, "pay_type", "") or "")
    evidence = [
        int(value)
        for value in (getattr(deal, "requested_payment_evidence_ids", None) or [])
        if str(value).isdigit()
    ]
    default_pay_type = deal.__class__.PayType.ONLINE_FULL
    if evidence or pay_type != default_pay_type:
        return [
            paylink,
            _node(
                "pay_type",
                _STATUS.COMPLETE,
                typed_value={"pay_type": pay_type},
                evidence_message_ids=tuple(evidence),
                closure_method=ClosureMethod.DEAL_PAY_TYPE,
            ),
        ]
    # Типове `online_full` без жодного evidence — те саме, що з кількістю:
    # значення робоче, вибору клієнта за ним не видно.
    return [
        paylink,
        _node(
            "pay_type",
            _STATUS.PARTIAL,
            reason_code=Reason.ASSUMED_DEFAULT,
            typed_value={"pay_type": pay_type, "assumed": True},
            closure_method=ClosureMethod.DEAL_PAY_TYPE,
        ),
    ]


_CONFIGURATION_KEYS = (
    "fit", "color", "size", "size_named", "size_available", "option_axes", "quantity",
)


def project_nodes(*, readiness: dict, deal=None) -> FunnelProjection:
    """Порахувати стан усіх вузлів із наявних фактів.

    Функція **не робить жодного запиту до БД**, коли `readiness` і `deal` уже
    передані: метрика етапу — «число SQL-запросов на расчёт узлов не должно
    вырасти», і єдиний спосіб це гарантувати — не мати запитів узагалі.
    `IgClient.stage` не читається: закриття доводиться даними.
    """
    readiness = readiness if isinstance(readiness, dict) else {}
    nodes: list[NodeProjection] = [_project_product(readiness)]
    if readiness.get("has_product"):
        nodes.append(_project_fit(readiness))
        nodes.append(_project_color(readiness))
        nodes.extend(_project_size(readiness))
        nodes.append(_project_option_axes(readiness))
        nodes.append(_project_quantity(readiness))
    else:
        nodes.extend(_awaiting_product(key) for key in _CONFIGURATION_KEYS)
    nodes.extend(_project_payment(readiness, deal))
    nodes.extend(_project_delivery(deal))

    by_key = {node.key: node for node in nodes}
    missing_keys = tuple(set(MATERIALIZED_NODE_KEYS) - by_key.keys())
    if missing_keys:
        raise RegistryError(
            "проєкція не покрила вузли: " + ", ".join(sorted(missing_keys))
        )
    payable_ready = bool(readiness.get("can_issue_link"))
    gaps = _blocking_gaps(by_key, IrreversibleAction.PAY_LINK_ISSUE)
    return FunnelProjection(
        nodes=tuple(by_key[key] for key in MATERIALIZED_NODE_KEYS),
        payable_ready=payable_ready,
        # Авторитет лишається за `checkout_readiness`; розходження — це факт,
        # який видно, а не привід перекрити його проєкцією.
        authority_agrees=bool(gaps) != payable_ready,
    )


def project_for_client(client, *, product_id=None, deal=None) -> FunnelProjection:
    """Обгортка для викликачів, у яких ще немає готових фактів."""
    from management.services.ig_checkout_readiness import checkout_readiness

    readiness = checkout_readiness(client, product_id=product_id)
    if deal is None:
        deal_id = (readiness.get("link") or {}).get("deal_id")
        if deal_id:
            from management.models import IgDeal

            deal = IgDeal.objects.filter(pk=deal_id).first()
    return project_nodes(readiness=readiness, deal=deal)


def projection_mode() -> str:
    """`off` → нічого не пишеться; `shadow` → пишеться поруч із авторитетом;
    `enforce` → те саме плюс право читачів спиратись на проєкцію (Э4.3+).

    Відкат етапу — це саме цей флаг, а не видалення таблиці.
    """
    value = str(getattr(settings, PROJECTION_MODE_FLAG, MODE_OFF) or MODE_OFF).casefold()
    return value if value in {MODE_OFF, MODE_SHADOW, MODE_ENFORCE} else MODE_OFF


def _address_digest(branch_type: str, line_id: str, recipient_id: str) -> str:
    raw = "\x1f".join((str(branch_type), str(line_id), str(recipient_id)))
    return hashlib.blake2s(raw.encode("utf-8"), digest_size=6).hexdigest()


def node_key(
    *,
    client_id: int,
    definition_key: str,
    episode_id: int | None = None,
    branch_type: str = "",
    line_id: str = "",
    recipient_id: str = "",
) -> str:
    """Адреса рядка стану.

    Один рядок замість кортежного UniqueConstraint: `commercial_episode` nullable,
    а `line_id`/`recipient_id` часто порожні. У MariaDB два NULL в unique-індексі
    різні, тому кортежний constraint пропустив би дублікати саме для клієнтських
    вузлів. Гілка і лінія входять у ключ через digest, щоб довгі непрозорі
    ідентифікатори не вилізли за довжину поля.
    """
    branch = branch_type or IgFunnelNodeState.BranchType.MAIN
    digest = _address_digest(branch, line_id, recipient_id)
    return f"{int(client_id)}:{int(episode_id or 0)}:{definition_key}:{digest}"


_PERSISTED_FIELDS = (
    "definition_version",
    "status",
    "reason_code",
    "reason_detail",
    "invalidated_by_definition_key",
    "typed_value",
    "previous_typed_value",
    "evidence_message_ids",
    "closure_method",
    "projector_version",
    "observed_at",
    "invalidated_at",
)


def persist_projection(
    client,
    projection: FunnelProjection,
    *,
    episode=None,
    branch_type: str = "",
    line_id: str = "",
    recipient_id: str = "",
    now=None,
) -> dict:
    """Записати проєкцію в `IgFunnelNodeState`. Один SELECT, один INSERT, один UPDATE.

    Запис ідемпотентний за адресою вузла: повторний прогін без змін не створює
    рядків і не робить UPDATE. Це важливо не для краси, а для метрики: проєкція
    буде рахуватись на кожному ході, і бюджет запитів мусить бути постійним.
    """
    mode = projection_mode()
    if mode == MODE_OFF:
        return {"mode": mode, "created": 0, "updated": 0, "unchanged": 0}
    if not getattr(client, "pk", None):
        return {"mode": mode, "created": 0, "updated": 0, "unchanged": 0}
    semantic_keys = [node.key for node in projection.nodes if not node.definition.is_materialized]
    if semantic_keys:
        raise RegistryError(
            "semantic-only вузли не можна materialize: " + ", ".join(sorted(semantic_keys))
        )

    moment = now or timezone.now()
    branch = branch_type or IgFunnelNodeState.BranchType.MAIN
    episode_id = getattr(episode, "pk", None)
    keys = {
        node.key: node_key(
            client_id=client.pk,
            definition_key=node.key,
            episode_id=episode_id,
            branch_type=branch,
            line_id=line_id,
            recipient_id=recipient_id,
        )
        for node in projection.nodes
    }
    existing = {
        row.node_key: row
        for row in IgFunnelNodeState.objects.filter(node_key__in=list(keys.values()))
    }

    to_create: list[IgFunnelNodeState] = []
    to_update: list[IgFunnelNodeState] = []
    unchanged = 0
    for node in projection.nodes:
        row = existing.get(keys[node.key])
        invalidated_at = moment if node.status == _STATUS.INVALIDATED else None
        if row is None:
            to_create.append(IgFunnelNodeState(
                node_key=keys[node.key],
                client=client,
                commercial_episode=episode,
                branch_type=branch,
                line_id=line_id,
                recipient_id=recipient_id,
                definition_key=node.key,
                definition_version=node.definition.version,
                status=node.status,
                reason_code=node.reason_code,
                reason_detail=node.reason_detail,
                invalidated_by_definition_key=node.invalidated_by,
                typed_value=dict(node.typed_value),
                evidence_message_ids=list(node.evidence_message_ids),
                closure_method=node.closure_method,
                projector_version=projection.projector_version,
                observed_at=moment,
                invalidated_at=invalidated_at,
            ))
            continue
        value_changed = row.typed_value != dict(node.typed_value)
        if not value_changed and row.status == node.status and row.reason_code == node.reason_code:
            unchanged += 1
            continue
        if value_changed and row.typed_value:
            # Історія значень: Э4.2 вимагає не переспитувати те, що клієнт уже
            # сказав, а для цього треба бачити попереднє значення.
            row.previous_typed_value = row.typed_value
        row.definition_version = node.definition.version
        row.status = node.status
        row.reason_code = node.reason_code
        row.reason_detail = node.reason_detail
        row.invalidated_by_definition_key = node.invalidated_by
        row.typed_value = dict(node.typed_value)
        row.evidence_message_ids = list(node.evidence_message_ids)
        row.closure_method = node.closure_method
        row.projector_version = projection.projector_version
        row.observed_at = moment
        row.invalidated_at = invalidated_at
        to_update.append(row)

    if to_create:
        IgFunnelNodeState.objects.bulk_create(to_create)
    if to_update:
        IgFunnelNodeState.objects.bulk_update(to_update, list(_PERSISTED_FIELDS))
    return {
        "mode": mode,
        "created": len(to_create),
        "updated": len(to_update),
        "unchanged": unchanged,
    }
