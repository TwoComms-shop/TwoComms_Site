"""Э4.1 — одне канонічне визначення вузла воронки і проєкція наявних вузлів.

Тести написані так, щоб ловити рівно ті помилки, через які подібний реєстр
виглядає працюючим і не працює: статус-літерал, якого немає в enum; закриття,
виведене зі стадії; `not_applicable`, який насправді означає «не знаємо»; граф,
у якому посилання висить, а обхід тихо його пропускає.
"""
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from management.models import IgClient, IgDeal, IgFunnelNodeState
from management.services import ig_funnel_nodes as nodes
from management.services.ig_funnel_nodes import (
    DependencyKind,
    IrreversibleAction,
    NodeClass,
    NodeDependency,
    FunnelNodeDefinition,
    FunnelProjection,
    NodeProjection,
    REGISTRY,
    RegistryError,
    SemanticKind,
    SemanticTransition,
)

STATUS = IgFunnelNodeState.Status

# Рівно ті вузли, що вже існують: дванадцять із плану плюс дві під-умови `size`,
# які план вимагає виразити окремо. Тест падає при спробі завести новий вузол —
# головне правило етапу («ни одного нового узла») мусить мати зуби.
EXPECTED_KEYS = frozenset({
    "product", "fit", "size", "size_named", "size_available", "color",
    "option_axes", "quantity", "city", "branch", "phone", "recipient_name",
    "pay_type", "paylink",
})


def _definition(key: str, **overrides) -> FunnelNodeDefinition:
    base = dict(
        key=key,
        node_class=NodeClass.QUALITY,
        ui_label=key,
        group="test",
        applicable_when="always",
        evidence_policy=nodes.EvidencePolicy.CATALOG_FACT,
        projection_target=nodes.ProjectionTarget.CHECKOUT_READINESS,
    )
    base.update(overrides)
    return FunnelNodeDefinition(**base)


def _registry(*definitions) -> dict:
    return {definition.key: definition for definition in definitions}


def readiness_stub(**overrides) -> dict:
    """Знімок форми, яку віддає `checkout_readiness`, без запитів до каталогу."""
    state = {
        "has_product": True,
        "product": {"id": 7, "title": "Худі", "published": True},
        "fit": {"required": False, "selected": "", "options": []},
        "size": {
            "required": False, "selected": "", "available": [], "disabled": [],
            "requested_unavailable": "",
        },
        "color": {"required": False, "selected": "", "selected_variant_id": None, "options": []},
        "options": {"required": False, "selected": {}, "missing": [], "axes": []},
        "quantity": 1,
        "missing": [],
        "can_issue_link": True,
        "link": {"status": "none", "expires_at": None, "deal_id": None},
    }
    state.update(overrides)
    return state


class RegistryStaticCheckTests(SimpleTestCase):
    def test_registry_passes_static_check(self):
        self.assertEqual(nodes.validate_registry(), ())

    def test_only_existing_nodes_are_materialized(self):
        self.assertEqual(set(nodes.MATERIALIZED_NODE_KEYS), set(EXPECTED_KEYS))
        self.assertEqual(
            {definition.key for definition in nodes.semantic_definitions()},
            set(nodes.SEMANTIC_NODE_KEYS),
        )

    def test_d064_entries_and_shared_joins_are_structural_only(self):
        required_entries = {
            "inbound", "ad_resolved_product", "catalog_discovery", "photo_reference",
            "custom_print", "dtf_only", "prize_candidate", "information_question",
            "collaboration", "employment", "spam_confirmed", "post_sale_request",
        }
        self.assertTrue(required_entries.issubset(nodes.SEMANTIC_NODE_KEYS))
        self.assertTrue({"configured_line", "quoted_offer", "settlement", "fulfillment"}.issubset(
            nodes.SEMANTIC_NODE_KEYS
        ))
        self.assertTrue({"awaiting_payment", "payment_help"}.issubset(nodes.SEMANTIC_NODE_KEYS))
        for definition in nodes.semantic_definitions():
            self.assertFalse(definition.is_materialized)
            self.assertEqual(definition.node_class, NodeClass.CONTEXT)
            self.assertIn(definition.semantic_kind, SemanticKind.ALL)
            self.assertTrue(definition.route_keys)
            self.assertTrue(definition.authority_contract)
            self.assertIn(definition.evidence_policy, nodes.EvidencePolicy.ALL)
            self.assertEqual(definition.prompt_priority, 0)
            self.assertFalse(definition.dependencies)
            self.assertFalse(definition.blocking_for)

    def test_structural_transitions_are_typed_and_not_history(self):
        transitions = nodes.structural_transitions(("collaboration",))
        self.assertTrue(transitions)
        self.assertTrue(all(item.source_key in nodes.SEMANTIC_NODE_KEYS for item in transitions))
        self.assertTrue(all(item.target_key in REGISTRY for item in transitions))
        self.assertIn("collaboration_designer", {item.target_key for item in transitions})
        with self.assertRaises(RegistryError):
            nodes.semantic_definition_for("product")

    def test_payment_help_has_explicit_non_linear_correction_paths(self):
        transitions = {
            item.target_key
            for item in nodes.structural_transitions(("payment",))
            if item.source_key == "payment_help"
        }
        self.assertEqual(
            transitions,
            {"awaiting_payment", "quoted_offer", "configured_line", "objection_case"},
        )

    def test_post_purchase_contact_has_distinct_consent_capability_and_ugc_offer(self):
        transitions = nodes.structural_transitions()
        pairs = {(item.source_key, item.target_key) for item in transitions}
        self.assertIn(("settlement", "channel_consent"), pairs)
        self.assertIn(("fulfillment", "channel_consent"), pairs)
        self.assertIn(("channel_consent", "channel_grant_checked"), pairs)
        self.assertIn(("channel_grant_checked", "post_purchase_contact_offer"), pairs)
        self.assertIn(("post_purchase_contact_offer", "ugc_assessment"), pairs)
        self.assertIn(("fulfillment", "channel_grant_checked"), pairs)
        self.assertNotIn(("fulfillment", "post_purchase_contact_offer"), pairs)

    def test_custom_repeat_and_entitlement_paths_keep_their_requirements(self):
        paths = {(item.source_key, item.target_key): item.outcome for item in nodes.structural_transitions()}
        self.assertEqual(paths["mockup_current_acceptance", "configured_line"], "current_mockup_accepted")
        self.assertNotIn(("mockup_current_acceptance", "quoted_offer"), paths)
        self.assertEqual(paths["new_purchase_interest", "catalog_discovery"], "new_selection")
        self.assertEqual(paths["new_purchase_interest", "configured_line"], "current_configuration_confirmed")
        self.assertEqual(paths["quoted_offer", "settlement"], "verified_entitlement_covers_total")
        self.assertEqual(paths["quoted_offer", "awaiting_payment"], "payment_required")
        self.assertEqual(paths["ugc_assessment", "reward_entitlement"], "authorised_reward_grant")

    def test_new_payment_attempt_is_not_confirmed_settlement(self):
        attempts = [item for item in nodes.structural_transitions()
                    if item.source_key == "objection_case" and item.outcome == "new_attempt"]
        self.assertEqual([item.target_key for item in attempts], ["awaiting_payment"])

    def test_restock_consent_does_not_gate_passive_wait_or_imply_payment(self):
        routes = nodes.structural_transitions()
        consent = {(r.target_key, r.outcome) for r in routes if r.source_key == "restock_consent"}
        self.assertEqual(consent, {("stock_wait", "restock_consent_granted"),
                                   ("stock_wait", "restock_consent_not_granted")})
        waiting = {(r.target_key, r.outcome) for r in routes if r.source_key == "stock_wait"}
        self.assertIn(("configured_line", "stock_rechecked_available"), waiting)
        self.assertFalse(any(target in {"settlement", "fulfillment", "post_purchase_contact_offer"}
                             for target, _ in waiting | consent))
        from management.services.ig_journey_catalogue import journey_catalogue
        definitions = {d["key"]: d for d in journey_catalogue()["definitions"]}
        self.assertEqual(definitions["stock_wait"]["implementation_status"], "planned")
        self.assertEqual(definitions["restock_consent"]["implementation_status"], "planned")

    def test_semantic_transition_target_is_validated(self):
        broken = _registry(
            _definition(
                "semantic",
                node_class=NodeClass.CONTEXT,
                projection_target=nodes.ProjectionTarget.NONE,
                semantic_kind=SemanticKind.ENTRY,
                route_keys=("test",),
                requirement_role=nodes.RequirementRole.CONTEXT,
                authority_contract="test_source",
                transitions=(SemanticTransition("ghost"),),
            ),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(any("semantic transition" in problem for problem in problems), problems)

    def test_dangling_dependency_is_reported(self):
        broken = _registry(
            _definition("a", dependencies=(NodeDependency(DependencyKind.BLOCKS, "ghost"),)),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(any("висяче посилання" in problem for problem in problems), problems)

    def test_ordering_cycle_is_reported(self):
        broken = _registry(
            _definition("a", dependencies=(NodeDependency(DependencyKind.DETERMINES, "b"),)),
            _definition("b", dependencies=(NodeDependency(DependencyKind.DETERMINES, "a"),)),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(any("цикл" in problem for problem in problems), problems)

    def test_mutual_invalidation_is_allowed_because_the_catalog_works_that_way(self):
        # Колір відсікає розміри, а обраний розмір відсікає кольори. Заборона
        # взаємності змусила б приховати одне з двох правдивих ребер.
        self.assertIn("size", REGISTRY["color"].dependencies_of(DependencyKind.INVALIDATES))
        self.assertIn("color", REGISTRY["size"].dependencies_of(DependencyKind.INVALIDATES))
        self.assertEqual(nodes.validate_registry(), ())

    def test_fourth_level_of_nesting_is_rejected(self):
        broken = _registry(
            _definition("root"),
            _definition("child", parent_key="root"),
            _definition("grandchild", parent_key="child"),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(any("четвертий рівень" in problem for problem in problems), problems)

    def test_third_level_exists_only_where_two_independent_conditions_are_needed(self):
        parents = {
            definition.parent_key
            for definition in REGISTRY.values()
            if definition.parent_key
        }
        self.assertEqual(parents, {"size"})
        self.assertEqual(REGISTRY["color"].level, 2)
        self.assertEqual(REGISTRY["size_available"].level, 3)

    def test_blocking_for_names_only_irreversible_actions(self):
        for definition in REGISTRY.values():
            for action in definition.blocking_for:
                self.assertIn(action, IrreversibleAction.ALL, definition.key)
        broken = _registry(
            _definition(
                "a", node_class=NodeClass.BLOCKING, blocking_for=("collect_marketing_context",)
            ),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(
            any("не є необоротною дією" in problem for problem in problems), problems
        )

    def test_delivery_nodes_block_order_creation_but_not_the_pay_link(self):
        # `checkout_readiness.can_issue_link` не вимагає адреси, а
        # `bot_orders.fulfill_if_ready` вимагає. Один клас блокування на дві різні
        # дії злив би ці правила в одне неправдиве.
        for key in ("city", "branch", "phone", "recipient_name"):
            self.assertEqual(REGISTRY[key].blocking_for, (IrreversibleAction.ORDER_CREATE,))
        for key in ("product", "fit", "size", "color", "option_axes"):
            self.assertEqual(REGISTRY[key].blocking_for, (IrreversibleAction.PAY_LINK_ISSUE,))

    def test_graph_uses_exactly_four_dependency_kinds(self):
        used = {
            dependency.kind
            for definition in REGISTRY.values()
            for dependency in definition.dependencies
        }
        self.assertEqual(used, DependencyKind.ALL)
        broken = _registry(
            _definition("a"),
            _definition("b", dependencies=(NodeDependency("if_then", "a"),)),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(
            any("невідомий тип залежності" in problem for problem in problems), problems
        )

    def test_quality_nodes_cannot_block_an_irreversible_action(self):
        broken = _registry(
            _definition(
                "a", node_class=NodeClass.QUALITY,
                blocking_for=(IrreversibleAction.PAY_LINK_ISSUE,),
            ),
        )
        problems = nodes.validate_registry(broken)
        self.assertTrue(
            any("не має права блокувати" in problem for problem in problems), problems
        )

    def test_context_node_is_never_asked(self):
        broken = _registry(_definition("a", node_class=NodeClass.CONTEXT, prompt_priority=5))
        problems = nodes.validate_registry(broken)
        self.assertTrue(any("CONTEXT" in problem for problem in problems), problems)

    def test_skip_is_allowed_for_quality_with_reason_and_never_for_blocking(self):
        nodes.validate_skip("quantity", "hot_lead_fast_track")
        with self.assertRaises(RegistryError):
            nodes.validate_skip("quantity", "")
        with self.assertRaises(RegistryError):
            nodes.validate_skip("size", "hot_lead_fast_track")

    def test_ask_order_narrows_first_and_leaves_logistics_last(self):
        order = nodes.ask_order()
        self.assertEqual(order[0], "product")
        self.assertLess(order.index("fit"), order.index("size"))
        self.assertLess(order.index("color"), order.index("size"))
        self.assertLess(order.index("city"), order.index("branch"))
        for logistics in ("city", "branch", "recipient_name", "phone"):
            self.assertGreater(order.index(logistics), order.index("size"))
        # Наявність розміру і саме посилання не є питаннями до клієнта.
        self.assertNotIn("size_available", order)
        self.assertNotIn("paylink", order)

    def test_unknown_node_and_unknown_action_fail_loudly(self):
        with self.assertRaises(RegistryError):
            nodes.definition_for("gift_wrap")
        with self.assertRaises(RegistryError):
            nodes.project_nodes(readiness=readiness_stub()).blocking_gaps("send_message")

    def test_custom_registry_does_not_poison_the_cached_default_order(self):
        default_order = nodes.ask_order()
        custom = nodes.ask_order(_registry(_definition("only", prompt_priority=5)))
        self.assertEqual(custom, ("only",))
        self.assertEqual(nodes.ask_order(), default_order)
        self.assertIn("product", nodes.ask_order())


class ProjectionFromFactsTests(SimpleTestCase):
    """Проєкція — чиста функція від фактів `checkout_readiness` і полів угоди."""

    def test_status_vocabulary_is_the_model_enum(self):
        # Захист від тієї самої помилки, що вже траплялась: фільтр по рядку,
        # якого немає в enum, нічого не знаходить, і фіча тихо мертва.
        projection = nodes.project_nodes(readiness=readiness_stub())
        for node in projection.nodes:
            self.assertIn(node.status, STATUS.values)

    def test_every_materialized_node_is_projected(self):
        projection = nodes.project_nodes(readiness=readiness_stub())
        self.assertEqual(set(projection.by_key()), set(nodes.MATERIALIZED_NODE_KEYS))
        self.assertFalse(set(projection.by_key()).intersection(nodes.SEMANTIC_NODE_KEYS))

    def test_missing_product_leaves_configuration_open_not_not_applicable(self):
        readiness = readiness_stub(
            has_product=False, product=None, missing=["product"], can_issue_link=False,
        )
        by_key = nodes.project_nodes(readiness=readiness).by_key()
        self.assertEqual(by_key["product"].status, STATUS.OPEN)
        for key in ("fit", "color", "size", "option_axes"):
            self.assertEqual(by_key[key].status, STATUS.OPEN, key)
            self.assertEqual(by_key[key].reason_code, nodes.Reason.AWAITING_PRODUCT, key)

    def test_absent_axis_is_policy_based_not_applicable_with_a_reason(self):
        by_key = nodes.project_nodes(readiness=readiness_stub()).by_key()
        self.assertEqual(by_key["fit"].status, STATUS.NOT_APPLICABLE)
        self.assertEqual(by_key["fit"].reason_code, nodes.Reason.NO_FIT_AXIS)
        self.assertEqual(by_key["fit"].closure_method, nodes.ClosureMethod.CATALOG_POLICY)

    def test_single_variant_closes_color_as_policy_and_keeps_the_variant(self):
        readiness = readiness_stub(color={
            "required": False, "selected": "Чорний", "selected_variant_id": 12,
            "options": [{"variant_id": 12, "name": "Чорний"}],
        })
        color = nodes.project_nodes(readiness=readiness).by_key()["color"]
        self.assertEqual(color.status, STATUS.NOT_APPLICABLE)
        self.assertEqual(color.reason_code, nodes.Reason.SINGLE_VARIANT)
        self.assertEqual(color.typed_value["variant_id"], 12)

    def test_named_but_unavailable_size_splits_into_two_subconditions(self):
        readiness = readiness_stub(
            size={
                "required": True, "selected": "", "available": ["S", "M"],
                "disabled": ["L"], "requested_unavailable": "L",
            },
            missing=["size"],
            can_issue_link=False,
        )
        projection = nodes.project_nodes(readiness=readiness)
        by_key = projection.by_key()
        self.assertEqual(by_key["size_named"].status, STATUS.COMPLETE)
        self.assertEqual(by_key["size_available"].status, STATUS.INVALIDATED)
        self.assertEqual(
            by_key["size_available"].reason_code, nodes.Reason.REQUESTED_SIZE_UNAVAILABLE
        )
        self.assertEqual(by_key["size_available"].reason_detail, "L")
        # Гальмує саме доступність, а не «розмір» узагалі: тільки так бот може
        # сказати «ти назвав L, у цьому кольорі його немає», а не спитати заново.
        self.assertEqual(
            projection.blocking_gaps(IrreversibleAction.PAY_LINK_ISSUE), ("size_available",)
        )

    def test_unnamed_size_blocks_on_naming_not_on_availability(self):
        readiness = readiness_stub(
            size={
                "required": True, "selected": "", "available": ["S", "M"],
                "disabled": [], "requested_unavailable": "",
            },
            missing=["size"],
            can_issue_link=False,
        )
        projection = nodes.project_nodes(readiness=readiness)
        self.assertEqual(
            projection.blocking_gaps(IrreversibleAction.PAY_LINK_ISSUE), ("size_named",)
        )
        self.assertEqual(
            projection.by_key()["size_available"].reason_code, nodes.Reason.AWAITING_SIZE_NAMED
        )

    def test_partially_chosen_option_axes_are_partial_not_empty(self):
        readiness = readiness_stub(
            options={
                "required": True,
                "selected": {"material": "cotton"},
                "missing": ["print"],
                "axes": [
                    {"code": "material", "selected": "cotton", "choices": []},
                    {"code": "print", "selected": "", "choices": []},
                ],
            },
            missing=["option:print"],
            can_issue_link=False,
        )
        axes = nodes.project_nodes(readiness=readiness).by_key()["option_axes"]
        self.assertEqual(axes.status, STATUS.PARTIAL)
        self.assertEqual(axes.typed_value["missing"], ["print"])

    def test_stale_option_value_without_axes_still_blocks(self):
        # `checkout_readiness` додає `option:<code>` навіть коли осей уже немає.
        # Перевірка `required` першою дала б `not_applicable` там, де посилання
        # заблоковане, і проєкція почала б суперечити авторитету.
        readiness = readiness_stub(
            options={"required": False, "selected": {}, "missing": ["legacy"], "axes": []},
            missing=["option:legacy"],
            can_issue_link=False,
        )
        projection = nodes.project_nodes(readiness=readiness)
        self.assertEqual(projection.by_key()["option_axes"].status, STATUS.OPEN)
        self.assertTrue(projection.authority_agrees)

    def test_default_quantity_is_partial_because_nothing_proves_it(self):
        by_key = nodes.project_nodes(readiness=readiness_stub()).by_key()
        self.assertEqual(by_key["quantity"].status, STATUS.PARTIAL)
        self.assertEqual(by_key["quantity"].reason_code, nodes.Reason.ASSUMED_DEFAULT)
        by_key = nodes.project_nodes(readiness=readiness_stub(quantity=3)).by_key()
        self.assertEqual(by_key["quantity"].status, STATUS.COMPLETE)
        # І в обох випадках кількість нічого не блокує.
        self.assertNotIn(
            "quantity",
            nodes.project_nodes(readiness=readiness_stub()).blocking_gaps(
                IrreversibleAction.PAY_LINK_ISSUE
            ),
        )

    def test_expired_link_is_invalidated_and_unknown_ttl_is_partial(self):
        expired = nodes.project_nodes(
            readiness=readiness_stub(link={"status": "expired", "expires_at": None})
        ).by_key()["paylink"]
        self.assertEqual(expired.status, STATUS.INVALIDATED)
        self.assertEqual(expired.reason_code, nodes.Reason.LINK_EXPIRED)
        unknown = nodes.project_nodes(
            readiness=readiness_stub(link={"status": "unknown", "expires_at": None})
        ).by_key()["paylink"]
        self.assertEqual(unknown.status, STATUS.PARTIAL)
        live = nodes.project_nodes(
            readiness=readiness_stub(link={"status": "live", "expires_at": None})
        ).by_key()["paylink"]
        self.assertEqual(live.status, STATUS.COMPLETE)

    def test_delivery_without_a_deal_is_open_with_a_reason(self):
        by_key = nodes.project_nodes(readiness=readiness_stub(), deal=None).by_key()
        for key in ("city", "branch", "recipient_name", "phone"):
            self.assertEqual(by_key[key].status, STATUS.OPEN, key)
            self.assertEqual(by_key[key].reason_code, nodes.Reason.NO_DEAL, key)

    def test_display_address_without_directory_ref_is_partial(self):
        deal = IgDeal(np_city="Київ", np_office="Відділення 12")
        by_key = nodes.project_nodes(readiness=readiness_stub(), deal=deal).by_key()
        self.assertEqual(by_key["city"].status, STATUS.PARTIAL)
        self.assertEqual(by_key["city"].reason_code, nodes.Reason.AWAITING_DIRECTORY)
        self.assertEqual(by_key["branch"].status, STATUS.PARTIAL)
        # Незакритий вузол блокує саме створення замовлення, а не посилання —
        # рівно так, як `fulfill_if_ready` і `can_issue_link` розділені в коді.
        projection = nodes.project_nodes(readiness=readiness_stub(), deal=deal)
        self.assertIn("city", projection.blocking_gaps(IrreversibleAction.ORDER_CREATE))
        self.assertEqual(projection.blocking_gaps(IrreversibleAction.PAY_LINK_ISSUE), ())

    def test_directory_confirmed_address_closes_city_and_branch(self):
        from management.services.ig_delivery import DELIVERY_SOURCE_DIRECTORY

        deal = IgDeal(
            np_city="Київ",
            np_office="Відділення 12",
            np_settlement_ref="s-ref",
            np_city_ref="c-ref",
            np_warehouse_ref="w-ref",
            delivery_status=IgDeal.DeliveryStatus.VALIDATED,
            delivery_source=DELIVERY_SOURCE_DIRECTORY,
        )
        by_key = nodes.project_nodes(readiness=readiness_stub(), deal=deal).by_key()
        self.assertEqual(by_key["city"].status, STATUS.COMPLETE)
        self.assertEqual(by_key["branch"].status, STATUS.COMPLETE)
        self.assertEqual(by_key["city"].closure_method, nodes.ClosureMethod.NP_DIRECTORY)

    def test_branch_waits_for_the_city_instead_of_being_asked_first(self):
        deal = IgDeal(np_office="Відділення 12")
        by_key = nodes.project_nodes(readiness=readiness_stub(), deal=deal).by_key()
        self.assertEqual(by_key["branch"].reason_code, nodes.Reason.AWAITING_CITY)
        projection = nodes.project_nodes(readiness=readiness_stub(), deal=deal)
        self.assertNotIn("branch", projection.next_questions())
        self.assertIn("city", projection.next_questions())

    def test_personal_data_never_lands_in_typed_value(self):
        deal = IgDeal(np_full_name="Іван Петренко", np_phone="+380671112233")
        by_key = nodes.project_nodes(readiness=readiness_stub(), deal=deal).by_key()
        self.assertEqual(by_key["phone"].status, STATUS.COMPLETE)
        self.assertEqual(by_key["recipient_name"].status, STATUS.COMPLETE)
        payload = str(by_key["phone"].typed_value) + str(by_key["recipient_name"].typed_value)
        self.assertNotIn("380671112233", payload)
        self.assertNotIn("Петренко", payload)

    def test_pay_type_is_partial_until_evidence_or_a_non_default_choice(self):
        default_deal = IgDeal(pay_type=IgDeal.PayType.ONLINE_FULL)
        pay_type = nodes.project_nodes(readiness=readiness_stub(), deal=default_deal).by_key()
        self.assertEqual(pay_type["pay_type"].status, STATUS.PARTIAL)
        chosen = IgDeal(
            pay_type=IgDeal.PayType.ONLINE_FULL, requested_payment_evidence_ids=[41, 42]
        )
        projected = nodes.project_nodes(readiness=readiness_stub(), deal=chosen).by_key()
        self.assertEqual(projected["pay_type"].status, STATUS.COMPLETE)
        self.assertEqual(projected["pay_type"].evidence_message_ids, (41, 42))
        prepayment = IgDeal(pay_type=IgDeal.PayType.PREPAYMENT)
        projected = nodes.project_nodes(readiness=readiness_stub(), deal=prepayment).by_key()
        self.assertEqual(projected["pay_type"].status, STATUS.COMPLETE)

    def test_checkout_readiness_stays_the_authority_on_payable_readiness(self):
        cases = (
            readiness_stub(),
            readiness_stub(has_product=False, product=None, missing=["product"],
                           can_issue_link=False),
            readiness_stub(fit={"required": True, "selected": "", "options": [
                {"code": "regular", "label": "Regular"},
                {"code": "oversize", "label": "Oversize"},
            ]}, missing=["fit"], can_issue_link=False),
            readiness_stub(color={"required": True, "selected": "", "selected_variant_id": None,
                                  "options": [{"variant_id": 1, "name": "Чорний"},
                                              {"variant_id": 2, "name": "Білий"}]},
                           missing=["color"], can_issue_link=False),
            readiness_stub(size={"required": True, "selected": "M", "available": ["M"],
                                 "disabled": [], "requested_unavailable": ""}),
            readiness_stub(options={"required": True, "selected": {}, "missing": [],
                                    "axes": [{"code": "material", "selected": "cotton",
                                              "choices": []}]}),
        )
        for readiness in cases:
            projection = nodes.project_nodes(readiness=readiness)
            self.assertEqual(projection.payable_ready, bool(readiness["can_issue_link"]))
            self.assertTrue(
                projection.authority_agrees,
                f"проєкція розійшлась з authority: {projection.open_keys()}",
            )
            self.assertEqual(
                bool(projection.blocking_gaps(IrreversibleAction.PAY_LINK_ISSUE)),
                bool(readiness["missing"]),
            )

    def test_option_context_failure_blocks_and_is_not_called_not_applicable(self):
        readiness = readiness_stub(
            options={"required": False, "selected": {}, "missing": [], "axes": [],
                     "error": "unavailable"},
            missing=["options_unavailable"],
            can_issue_link=False,
        )
        projection = nodes.project_nodes(readiness=readiness)
        axes = projection.by_key()["option_axes"]
        self.assertEqual(axes.status, STATUS.OPEN)
        self.assertEqual(axes.reason_code, nodes.Reason.OPTION_CONTEXT_UNAVAILABLE)
        self.assertTrue(projection.authority_agrees)

    def test_unpublished_product_is_invalidated_not_merely_unselected(self):
        readiness = readiness_stub(
            has_product=False,
            product={"id": 7, "title": "Худі", "published": False},
            missing=["product"],
            can_issue_link=False,
        )
        product = nodes.project_nodes(readiness=readiness).by_key()["product"]
        self.assertEqual(product.status, STATUS.INVALIDATED)
        self.assertEqual(product.reason_code, nodes.Reason.CATALOG_UNPUBLISHED)


@override_settings(IG_FUNNEL_NODE_PROJECTION_MODE="shadow")
class NodeStatePersistenceTests(TestCase):
    def setUp(self):
        self.client_row = IgClient.get_or_create_for_sender("funnel-node-sender")

    def test_projection_itself_costs_no_queries(self):
        # Метрика етапу — число SQL-запитів на розрахунок вузлів не мусить
        # зростати. Єдина перевірка, яку не можна обійти: їх нуль.
        deal = IgDeal(np_city="Київ")
        readiness = readiness_stub()
        with self.assertNumQueries(0):
            nodes.project_nodes(readiness=readiness, deal=deal)

    def test_first_write_creates_one_row_per_node_and_repeat_writes_nothing(self):
        projection = nodes.project_nodes(readiness=readiness_stub())
        created = nodes.persist_projection(self.client_row, projection)
        self.assertEqual(created["created"], len(nodes.MATERIALIZED_NODE_KEYS))
        self.assertEqual(
            IgFunnelNodeState.objects.filter(client=self.client_row).count(), len(nodes.MATERIALIZED_NODE_KEYS)
        )
        again = nodes.persist_projection(self.client_row, projection)
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["updated"], 0)
        self.assertEqual(again["unchanged"], len(nodes.MATERIALIZED_NODE_KEYS))

    def test_repeat_write_stays_within_a_bounded_query_budget(self):
        projection = nodes.project_nodes(readiness=readiness_stub())
        nodes.persist_projection(self.client_row, projection)
        with self.assertNumQueries(1):
            nodes.persist_projection(self.client_row, projection)

    def test_off_mode_writes_nothing_so_rollback_is_a_flag(self):
        projection = nodes.project_nodes(readiness=readiness_stub())
        with override_settings(IG_FUNNEL_NODE_PROJECTION_MODE="off"):
            with self.assertNumQueries(0):
                result = nodes.persist_projection(self.client_row, projection)
        self.assertEqual(result["created"], 0)
        self.assertFalse(IgFunnelNodeState.objects.exists())

    def test_semantic_definition_cannot_be_persisted_as_node_state(self):
        definition = nodes.semantic_definition_for("inbound")
        projection = FunnelProjection(
            nodes=(NodeProjection(definition=definition, status=STATUS.OPEN),),
            payable_ready=False,
            authority_agrees=False,
        )
        with self.assertRaises(RegistryError):
            nodes.persist_projection(self.client_row, projection)
        self.assertFalse(IgFunnelNodeState.objects.exists())

    def test_changed_value_keeps_the_previous_one(self):
        first = nodes.project_nodes(readiness=readiness_stub(
            size={"required": True, "selected": "M", "available": ["M", "L"],
                  "disabled": [], "requested_unavailable": ""},
        ))
        nodes.persist_projection(self.client_row, first)
        second = nodes.project_nodes(readiness=readiness_stub(
            size={"required": True, "selected": "L", "available": ["M", "L"],
                  "disabled": [], "requested_unavailable": ""},
        ))
        nodes.persist_projection(self.client_row, second)
        row = IgFunnelNodeState.objects.get(
            client=self.client_row, definition_key="size"
        )
        self.assertEqual(row.typed_value, {"size": "L"})
        self.assertEqual(row.previous_typed_value, {"size": "M"})

    def test_invalidated_row_records_when_it_happened(self):
        projection = nodes.project_nodes(readiness=readiness_stub(
            size={"required": True, "selected": "", "available": ["S"],
                  "disabled": ["L"], "requested_unavailable": "L"},
            missing=["size"], can_issue_link=False,
        ))
        nodes.persist_projection(self.client_row, projection)
        row = IgFunnelNodeState.objects.get(
            client=self.client_row, definition_key="size_available"
        )
        self.assertEqual(row.status, STATUS.INVALIDATED)
        self.assertIsNotNone(row.invalidated_at)
        self.assertFalse(row.is_closed)
        self.assertFalse(row.is_terminal)

    def test_node_key_separates_episodes_branches_and_recipients(self):
        base = dict(client_id=self.client_row.pk, definition_key="size")
        keys = {
            nodes.node_key(**base),
            nodes.node_key(**base, episode_id=5),
            nodes.node_key(**base, branch_type=IgFunnelNodeState.BranchType.GIFT),
            nodes.node_key(**base, recipient_id="r-1"),
            nodes.node_key(**base, line_id="l-1"),
        }
        self.assertEqual(len(keys), 5)
        for key in keys:
            self.assertLessEqual(
                len(key), IgFunnelNodeState._meta.get_field("node_key").max_length
            )

    def test_reasonless_skip_or_not_applicable_is_refused_by_the_database(self):
        for status in (STATUS.SKIPPED, STATUS.NOT_APPLICABLE):
            with self.subTest(status=status):
                with self.assertRaises(IntegrityError):
                    with transaction.atomic():
                        IgFunnelNodeState.objects.create(
                            node_key=f"reasonless-{status}",
                            client=self.client_row,
                            definition_key="quantity",
                            status=status,
                        )

    def test_closure_is_never_inferred_from_stage(self):
        # Стадія каже «оплачено», а даних немає. Реєстр мусить це показати:
        # закриття доводиться фактами, `IgClient.stage` не розширюється.
        self.client_row.stage = IgClient.Stage.PAID
        self.client_row.save(update_fields=["stage"])
        projection = nodes.project_for_client(self.client_row)
        by_key = projection.by_key()
        self.assertEqual(by_key["product"].status, STATUS.OPEN)
        self.assertEqual(by_key["paylink"].status, STATUS.OPEN)
        self.assertFalse(projection.payable_ready)
        self.assertIn("product", projection.blocking_gaps(IrreversibleAction.PAY_LINK_ISSUE))

    def test_schema_carries_the_unique_address_and_the_reason_constraint(self):
        from django.db import connection

        table = IgFunnelNodeState._meta.db_table
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, table)
        unique_on_key = [
            name for name, meta in constraints.items()
            if meta.get("unique") and meta.get("columns") == ["node_key"]
        ]
        self.assertTrue(unique_on_key, constraints.keys())
        self.assertIn("ig_fnode_reason_required", constraints)
