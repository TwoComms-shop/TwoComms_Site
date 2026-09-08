from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from management.models import (
    IgClient, IgCommercialEpisode, IgCommerceSelectionSession,
    IgCommerceSelectionTransition, IgFunnelResetAudit, InstagramBotMessage,
)
from management.services import ig_journey_readiness as service
from management.tests_ig_funnel_nodes import readiness_stub


def facts(**overrides):
    return readiness_stub(applicability_known=True, **overrides)


class RequirementCountingTests(SimpleTestCase):
    def project(self, state, count=1):
        return service.requirements_from_readiness(state, scope={
            "active_position": 2 if count > 1 else 1, "line_count": count,
        }, evidence_refs=[{"kind": "message", "id": 10}])

    def test_proven_inapplicable_excluded_and_quality_never_counted(self):
        result = self.project(facts(quantity=9))["requirements"]
        self.assertEqual((result["completed"], result["total"]), (1, 1))
        self.assertNotIn("quantity", [row["key"] for row in result["items"]])
        self.assertNotIn("size", [row["key"] for row in result["items"]])

    def test_unavailable_named_size_counts_once_and_partial_option_group_not_complete(self):
        state = facts(
            fit={"required": True, "selected": "classic"},
            color={"required": True, "selected_variant_id": 8, "options": [{"variant_id": 8}, {"variant_id": 9}]},
            size={"required": True, "selected": "", "requested_unavailable": "M", "available": ["L"]},
            options={"required": True, "missing": ["material"], "axes": [{"code": "print", "selected": "front"}]},
            missing=["size", "option:material"], can_issue_link=False,
        )
        result = self.project(state)["requirements"]
        self.assertEqual((result["completed"], result["total"]), (4, 6))
        self.assertEqual(next(x for x in result["items"] if x["key"] == "option_axes")["status"], "partial")

    def test_unknown_applicability_and_authority_disagreement_hide_counter(self):
        state = facts()
        state.pop("applicability_known")
        self.assertIsNone(self.project(state)["requirements"])
        self.assertEqual(self.project(facts(can_issue_link=False))["reason"], "authority_disagrees")

    def test_strict_size_fallback_does_not_touch_image_storage(self):
        from types import SimpleNamespace
        from management.services.ig_checkout_readiness import sizes_for_fit
        with patch("storefront.services.size_guides._ordered_size_values_from_catalog", return_value=[]), \
             patch("storefront.services.size_guides._resolve_size_grid_source", return_value=(SimpleNamespace(image="private.png"), "product")), \
             patch("storefront.services.size_guides.resolve_product_sizes") as general:
            with self.assertRaisesRegex(ValueError, "image_backed"):
                sizes_for_fit(SimpleNamespace(pk=7), "", strict=True)
        general.assert_not_called()

    def test_multi_line_scope_labels_active_position(self):
        result = self.project(facts(), count=3)["requirements"]
        self.assertIn("позиція 2 із 3", result["label"])
        self.assertEqual(result["total"], 1)


class CurrentSelectionRequirementsTests(TestCase):
    def setUp(self):
        self.customer = IgClient.get_or_create_for_sender("journey-readiness")
        self.episode = IgCommercialEpisode.objects.create(client=self.customer, sequence=1, materialization_key="readiness-1")
        self.customer.current_commercial_episode = self.episode
        self.customer.save(update_fields=["current_commercial_episode"])
        self.line = {"line_id": "line:0", "product_id": 7, "size": "M"}
        self.session = IgCommerceSelectionSession.objects.create(
            client=self.customer, commercial_episode=self.episode, generation=1,
            lines=[self.line], revision=1,
        )
        self.source = InstagramBotMessage.objects.create(client=self.customer, sender_id="journey-readiness", role="user", text="M")
        after = self.session.snapshot()
        before = deepcopy(after)
        before.update(lines=[], revision=0)
        self.transition = IgCommerceSelectionTransition.objects.create(
            session=self.session, source_message=self.source, action="product_selected",
            from_revision=0, to_revision=1, previous_snapshot=before,
            next_snapshot=after, source_order_key="1",
        )

    def read(self, **kwargs):
        return service.selection_requirements(client_id=self.customer.pk, episode_id=self.episode.pk, **kwargs)

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_current_source_and_exact_eight_scope_reads(self, catalog):
        with self.assertNumQueries(8):
            result = self.read()
        self.assertEqual(result["requirements"]["completed"], 1)
        self.assertEqual(result["requirements"]["scope"]["line_id"], "line:0")
        self.assertEqual(catalog.call_args.kwargs["size"], "M")
        self.assertNotIn("client", catalog.call_args.kwargs)

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_foreign_line_and_historical_episode_do_not_read_catalog(self, catalog):
        self.assertEqual(self.read(line_id="other")["reason"], "not_active_line")
        self.assertEqual(service.selection_requirements(client_id=self.customer.pk, episode_id=self.episode.pk + 1)["reason"], "not_current_episode")
        catalog.assert_not_called()

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_explicit_reset_rejects_old_selection(self, catalog):
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk)
        self.assertEqual(self.read()["reason"], "no_owned_current_source")
        catalog.assert_not_called()

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_unrelated_post_reset_turn_does_not_rehabilitate_old_values(self, catalog):
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=self.source.pk)
        before = self.session.snapshot()
        self.session.revision = 2
        self.session.save(update_fields=["revision"])
        fresh = InstagramBotMessage.objects.create(client=self.customer, sender_id="journey-readiness", role="user", text="hello")
        IgCommerceSelectionTransition.objects.create(session=self.session, source_message=fresh, action="turn_unresolved",
            from_revision=1, to_revision=2, previous_snapshot=before, next_snapshot=self.session.snapshot(), source_order_key="2")
        self.assertEqual(self.read()["reason"], "no_owned_current_source")
        catalog.assert_not_called()

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_foreign_source_and_revisionless_snapshot_edit_rejected(self, catalog):
        other = IgClient.get_or_create_for_sender("other-readiness")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(client=other)
        self.assertEqual(self.read()["reason"], "no_owned_current_source")
        InstagramBotMessage.objects.filter(pk=self.source.pk).update(client=self.customer)
        IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(lines=[{**self.line, "size": "L"}])
        self.assertEqual(self.read()["reason"], "no_owned_current_source")
        catalog.assert_not_called()

    def test_concurrent_permission_session_and_source_change_hide_counter(self):
        mutations = [
            lambda: IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=1),
            lambda: IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(revision=2),
            lambda: InstagramBotMessage.objects.filter(pk=self.source.pk).update(client=None),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                def read_catalog(**kwargs):
                    # Mutation represents another worker, outside the adapter's
                    # real read-only catalog connection wrapper.
                    with patch.object(service.connection, "execute_wrappers", []):
                        mutation()
                    return facts()
                with patch.object(service, "selection_readiness", side_effect=read_catalog):
                    self.assertIsNone(self.read()["requirements"])
                IgClient.objects.filter(pk=self.customer.pk).update(reply_permission_epoch=0)
                IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(revision=1)
                InstagramBotMessage.objects.filter(pk=self.source.pk).update(client=self.customer)

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_erasure_and_bootstrap_without_transition_abstain(self, catalog):
        from django.utils import timezone
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=timezone.now())
        self.assertEqual(self.read()["reason"], "client_unavailable")
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=None)
        IgCommerceSelectionSession.objects.filter(pk=self.session.pk).update(revision=0)
        self.assertEqual(self.read()["reason"], "no_owned_current_source")
        catalog.assert_not_called()

    @patch.object(service, "selection_readiness", return_value=facts())
    def test_multi_line_uses_only_active_owned_position(self, catalog):
        before = self.session.snapshot()
        self.session.lines = [self.line, {"line_id": "line:1", "product_id": 8, "size": "L"}]
        self.session.active_index = 1
        self.session.revision = 2
        self.session.save(update_fields=["lines", "active_index", "revision"])
        source = InstagramBotMessage.objects.create(client=self.customer, sender_id="journey-readiness", role="user", text="another L")
        IgCommerceSelectionTransition.objects.create(session=self.session, source_message=source, action="product_selected",
            from_revision=1, to_revision=2, previous_snapshot=before, next_snapshot=self.session.snapshot(), source_order_key="2")
        result = self.read(line_id="line:1")["requirements"]
        self.assertIn("позиція 2 із 2", result["label"])
        self.assertEqual(catalog.call_args.kwargs["product_id"], 8)
        self.assertEqual(catalog.call_args.kwargs["size"], "L")

    def test_actual_catalog_read_budget_is_hard_bounded(self):
        executed = []
        def observe(execute, sql, params, many, context):
            executed.append(sql)
            return execute(sql, params, many, context)
        def expensive(**kwargs):
            with connection.execute_wrapper(observe), connection.cursor() as cursor:
                for _ in range(100):
                    cursor.execute("SELECT 1")
            return facts()
        with patch.object(service, "selection_readiness", side_effect=expensive):
            with CaptureQueriesContext(connection) as queries:
                result = self.read()
        self.assertEqual(result["reason"], "catalog_read_budget")
        self.assertEqual(result["catalog_reads"], 24)
        self.assertEqual(len(executed), 24)
        # Django debug logging also records the rejected 25th attempt.
        self.assertEqual(sum(q["sql"] == "SELECT 1" for q in queries), 25)

    def test_real_simple_catalog_reads_are_measured_without_legacy_inputs(self):
        from productcolors.models import Color, ProductColorVariant
        from storefront.models import Category, Product, ProductStatus
        category = Category.objects.create(name="Футболки", slug="journey-readiness")
        product = Product.objects.create(title="Футболка", slug="journey-readiness", category=category, price=880, status=ProductStatus.PUBLISHED)
        color = Color.objects.create(name="Чорний", primary_hex="#111111")
        variant = ProductColorVariant.objects.create(product=product, color=color, stock=0)
        from management.services.ig_checkout_readiness import selection_readiness
        budget = service._ReadBudget()
        with CaptureQueriesContext(connection) as queries:
            with connection.execute_wrapper(budget):
                try:
                    state = selection_readiness(product_id=product.pk, selection={"color_variant_id": variant.pk}, size="M", strict=True)
                except service._CatalogBudgetExceeded:
                    state = None
        self.assertLessEqual(budget.reads, 24)
        self.assertEqual(len(queries), budget.reads)
        print(f"journey readiness simple catalog: {budget.reads} actual SELECTs; budget_exceeded={budget.exceeded}")
        if not budget.exceeded:
            self.assertTrue(state["applicability_known"])

        # Match the existing CheckoutReadinessNoteTests fixture: two active
        # fits and one variant, with the classic fit explicitly selected.
        from storefront.models import ProductFitOption
        for code in ("classic", "oversize"):
            ProductFitOption.objects.create(product=product, code=code, label=code, is_active=True)
        fit_budget = service._ReadBudget()
        with connection.execute_wrapper(fit_budget):
            try:
                selection_readiness(product_id=product.pk, selection={"color_variant_id": variant.pk, "fit_option_code": "classic"}, size="M", strict=True)
            except service._CatalogBudgetExceeded:
                pass
        self.assertLessEqual(fit_budget.reads, 24)
        print(f"journey readiness two-fit catalog: {fit_budget.reads} actual SELECTs; budget_exceeded={fit_budget.exceeded}")
