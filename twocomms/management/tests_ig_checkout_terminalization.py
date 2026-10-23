"""Regression contract for local assisted-payment terminalization.

These tests deliberately avoid provider I/O.  A local deadline or an explicit
checkout-session cancellation is operational evidence, not provider payment
truth; a later provider-verified success must therefore remain recoverable.
"""

from __future__ import annotations

import hashlib
import importlib
import threading
from io import StringIO
from types import SimpleNamespace
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, connections
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (
    IgCheckoutInventoryReservation,
    IgCheckoutProposal,
    IgCheckoutProposalItem,
    IgClient,
    IgDeal,
    IgPaymentEvent,
    IgPaymentProjection,
)
from management.services.ig_commercial_episodes import ensure_episode_for_deal
from orders.models import Order, PaymentAttempt
from productcolors.models import Color, ProductColorVariant
from storefront.models import (
    Category,
    Product,
    ProductFitOption,
    PromoCode,
    PromoCodeUsage,
)


class AssistedAttemptTerminalizationTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.category = Category.objects.create(
            name="Terminalization",
            slug="ig-terminalization",
        )
        self.product = Product.objects.create(
            title="Terminalization shirt",
            slug="ig-terminalization-shirt",
            category=self.category,
            price=Decimal("900.00"),
            status="published",
        )
        self.color = Color.objects.create(
            name="Terminal black",
            primary_hex="#111111",
        )
        self.variant = ProductColorVariant.objects.create(
            product=self.product,
            color=self.color,
            stock=1,
        )
        self.fit = ProductFitOption.objects.create(
            product=self.product,
            code="classic",
            label="Classic",
            is_default=True,
        )
        self.user = get_user_model().objects.create_user(
            username="ig-terminalization-user",
        )
        self.sequence = 0

    def _graph(
        self,
        *,
        invoice_expires_at,
        age=timedelta(minutes=1),
        status=PaymentAttempt.Status.PROCESSING,
        ambiguous=False,
        with_inventory=False,
        with_promo=False,
        with_order=False,
    ):
        self.sequence += 1
        suffix = str(self.sequence)
        client = IgClient.get_or_create_for_sender(f"terminalization-{suffix}")
        deal = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.AWAITING_PAYMENT,
            amount=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
        )
        episode = ensure_episode_for_deal(deal)
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            commercial_episode=episode,
            catalog_total=Decimal("900.00"),
            quoted_total=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
            items_digest=hashlib.sha256(f"proposal-{suffix}".encode()).hexdigest(),
            expires_at=self.now + timedelta(days=1),
        )
        item = IgCheckoutProposalItem.objects.create(
            proposal=proposal,
            product=self.product,
            color_variant=self.variant,
            product_title=self.product.title,
            size="S",
            fit_code=self.fit.code,
            fit_label=self.fit.label,
            quantity=1,
            catalog_unit_price=Decimal("900.00"),
            catalog_line_total=Decimal("900.00"),
            quoted_unit_price=Decimal("900.00"),
            quoted_line_total=Decimal("900.00"),
        )
        promo = None
        event_state = {}
        if ambiguous:
            event_state["invoice_creation_ambiguous"] = True
        if with_promo:
            promo = PromoCode.objects.create(
                code=f"TERM{suffix}",
                discount_type="percentage",
                discount_value=Decimal("5.00"),
                max_uses=10,
                current_uses=1,
            )
            event_state["promo_reservation"] = {
                "promo_id": promo.pk,
                "state": "reserved",
                "capacity_reserved": True,
                "reserved_at": (self.now - age).isoformat(),
            }
        order = None
        if with_order:
            order = Order.objects.create(
                full_name="Already paid",
                phone="+380501112233",
                city="Kyiv",
                np_office="Branch 1",
                pay_type="online_full",
                payment_status="paid",
                total_sum=Decimal("900.00"),
            )
        attempt = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(f"attempt-{suffix}".encode()).hexdigest(),
            user=self.user if with_promo else None,
            session_key=f"session-{suffix}",
            full_name="Instagram Buyer",
            phone="+380501112233",
            city="Kyiv",
            np_office="Branch 1",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=status,
            cart_snapshot={
                "checkout_surface": "instagram_proposal",
                "sale_source": "Instagram",
                "proposal_id": str(proposal.public_id),
                "cart": [
                    {
                        "product_id": self.product.pk,
                        "title": self.product.title,
                        "qty": 1,
                        "size": "S",
                        "fit_option_code": self.fit.code,
                        "fit_option_label": self.fit.label,
                        "color_variant_id": self.variant.pk,
                        "option_values": {},
                        "option_labels": {},
                        "unit_price": "900.00",
                        "line_total": "900.00",
                    }
                ],
            },
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            promo_code=promo,
            event_state=event_state,
            monobank_invoice_id=f"invoice-{suffix}",
            invoice_url=f"https://pay.example/{suffix}",
            invoice_expires_at=invoice_expires_at,
            order=order,
        )
        PaymentAttempt.objects.filter(pk=attempt.pk).update(created=self.now - age)
        attempt.refresh_from_db()
        proposal.payment_attempt = attempt
        proposal.status = IgCheckoutProposal.Status.INVOICE_CREATED
        proposal.save(update_fields=["payment_attempt", "status", "updated_at"])
        reservation = None
        if with_inventory:
            from management.services.ig_inventory import _fingerprint

            allocation_key = f"catalog_variant:variant:{self.variant.pk}"
            reservation = IgCheckoutInventoryReservation.objects.create(
                proposal=proposal,
                item=item,
                product=self.product,
                color_variant=self.variant,
                allocation_source="catalog_variant",
                allocation_key=allocation_key,
                line_ids=[item.pk],
                quantity=1,
                reservation_fingerprint=_fingerprint(proposal, allocation_key),
                expires_at=self.now - timedelta(seconds=1),
            )
        return {
            "client": client,
            "deal": deal,
            "proposal": proposal,
            "attempt": attempt,
            "reservation": reservation,
            "promo": promo,
        }

    def test_explicit_invoice_deadline_terminalizes_immediately(self):
        graph = self._graph(
            invoice_expires_at=self.now - timedelta(seconds=1),
            age=timedelta(minutes=1),
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        result = expire_due_assisted_attempts(now=self.now, limit=10)

        graph["attempt"].refresh_from_db()
        graph["proposal"].refresh_from_db()
        self.assertEqual(result["expired_attempts"], 1)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.EXPIRED)
        self.assertEqual(graph["attempt"].error_reason, "invoice_expired")
        self.assertEqual(graph["proposal"].status, IgCheckoutProposal.Status.EXPIRED)
        self.assertIsNone(graph["proposal"].invoice_cancelled_at)
        self.assertIsNone(graph["proposal"].provider_cancellation_event_id)
        graph["deal"].refresh_from_db()
        self.assertIsNone(graph["deal"].active_checkout_proposal_id)
        self.assertEqual(graph["deal"].payment_truth, IgDeal.PaymentTruth.UNVERIFIED)
        self.assertFalse(IgPaymentEvent.objects.filter(deal=graph["deal"]).exists())
        self.assertFalse(IgPaymentProjection.objects.filter(deal=graph["deal"]).exists())
        self.assertEqual(
            graph["attempt"].event_state["local_terminalization"]["source"],
            "system_expiry",
        )
        self.assertFalse(
            graph["attempt"].event_state["local_terminalization"][
                "provider_truth_changed"
            ]
        )

    def test_null_expiry_uses_only_the_24_hour_legacy_fallback(self):
        recent = self._graph(
            invoice_expires_at=None,
            age=timedelta(hours=23, minutes=59),
        )
        old = self._graph(
            invoice_expires_at=None,
            age=timedelta(hours=24, seconds=1),
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        result = expire_due_assisted_attempts(now=self.now, limit=10)

        recent["attempt"].refresh_from_db()
        old["attempt"].refresh_from_db()
        self.assertEqual(result["expired_attempts"], 1)
        self.assertEqual(recent["attempt"].status, PaymentAttempt.Status.PROCESSING)
        self.assertEqual(old["attempt"].status, PaymentAttempt.Status.EXPIRED)

    def test_expiry_releases_inventory_and_promo_capacity(self):
        graph = self._graph(
            invoice_expires_at=self.now,
            with_inventory=True,
            with_promo=True,
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        result = expire_due_assisted_attempts(now=self.now, limit=10)

        graph["attempt"].refresh_from_db()
        graph["reservation"].refresh_from_db()
        graph["promo"].refresh_from_db()
        self.assertEqual(result["released_inventory"], 1)
        self.assertEqual(result["released_promos"], 1)
        self.assertEqual(
            graph["reservation"].state,
            IgCheckoutInventoryReservation.State.RELEASED,
        )
        self.assertEqual(graph["promo"].current_uses, 0)
        self.assertEqual(
            graph["attempt"].event_state["promo_reservation"]["state"],
            "released",
        )

    def test_reconciler_is_the_runtime_owner_for_due_attempts(self):
        graph = self._graph(invoice_expires_at=self.now)

        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout

        result = reconcile_ig_checkout(limit=10, pull_ambiguous=False)

        graph["attempt"].refresh_from_db()
        self.assertEqual(result["expired_attempts"], 1)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.EXPIRED)

    def test_session_reset_uses_the_same_locked_terminalization_boundary(self):
        graph = self._graph(
            invoice_expires_at=self.now + timedelta(minutes=20),
            with_inventory=True,
            with_promo=True,
        )
        request = RequestFactory().get("/cart/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session["monobank_pending_attempt_id"] = graph["attempt"].pk
        request.session.save()

        from storefront.views.utils import _reset_monobank_session

        _reset_monobank_session(request, drop_pending=True)

        graph["attempt"].refresh_from_db()
        graph["reservation"].refresh_from_db()
        graph["promo"].refresh_from_db()
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.CANCELLED)
        self.assertEqual(graph["reservation"].state, IgCheckoutInventoryReservation.State.RELEASED)
        self.assertEqual(graph["promo"].current_uses, 0)
        self.assertEqual(
            graph["attempt"].event_state["local_terminalization"]["source"],
            "checkout_session_reset",
        )
        self.assertFalse(IgPaymentEvent.objects.filter(deal=graph["deal"]).exists())
        self.assertNotIn("monobank_pending_attempt_id", request.session)

    def test_terminalization_is_idempotent(self):
        graph = self._graph(
            invoice_expires_at=self.now,
            with_inventory=True,
            with_promo=True,
        )

        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        first = terminalize_payment_attempt(
            graph["attempt"].pk,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            now=self.now,
            require_due=True,
        )
        second = terminalize_payment_attempt(
            graph["attempt"].pk,
            terminal_status=PaymentAttempt.Status.EXPIRED,
            reason="invoice_expired",
            source="system_expiry",
            now=self.now,
            require_due=True,
        )

        self.assertEqual(first.outcome, "terminalized")
        self.assertEqual(second.outcome, "already_terminal")
        graph["attempt"].refresh_from_db()
        self.assertEqual(
            len(graph["attempt"].event_state["local_terminalization_events"]),
            1,
        )
        graph["promo"].refresh_from_db()
        self.assertEqual(graph["promo"].current_uses, 0)

    def test_crash_rolls_back_the_whole_boundary_and_replay_finishes(self):
        graph = self._graph(
            invoice_expires_at=self.now,
            with_inventory=True,
            with_promo=True,
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        with patch(
            "management.services.ig_checkout_terminalization.release_payment_attempt_promo",
            side_effect=RuntimeError("simulated crash"),
        ), patch(
            "management.services.ig_checkout_terminalization.logger.exception"
        ):
            failed = expire_due_assisted_attempts(now=self.now, limit=10)

        graph["attempt"].refresh_from_db()
        graph["proposal"].refresh_from_db()
        graph["reservation"].refresh_from_db()
        graph["promo"].refresh_from_db()
        self.assertEqual(failed["errors"], 1)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.PROCESSING)
        self.assertEqual(graph["proposal"].status, IgCheckoutProposal.Status.INVOICE_CREATED)
        self.assertEqual(graph["reservation"].state, IgCheckoutInventoryReservation.State.ACTIVE)
        self.assertEqual(graph["promo"].current_uses, 1)
        self.assertFalse(IgPaymentEvent.objects.filter(deal=graph["deal"]).exists())

        replay = expire_due_assisted_attempts(now=self.now, limit=10)
        graph["attempt"].refresh_from_db()
        self.assertEqual(replay["expired_attempts"], 1)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.EXPIRED)

    def test_ambiguous_and_paid_attempts_are_excluded(self):
        ambiguous = self._graph(
            invoice_expires_at=self.now,
            ambiguous=True,
        )
        paid = self._graph(
            invoice_expires_at=self.now,
            with_order=True,
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        result = expire_due_assisted_attempts(now=self.now, limit=10)

        ambiguous["attempt"].refresh_from_db()
        paid["attempt"].refresh_from_db()
        self.assertEqual(result["expired_attempts"], 0)
        self.assertEqual(ambiguous["attempt"].status, PaymentAttempt.Status.PROCESSING)
        self.assertIsNotNone(paid["attempt"].order_id)
        self.assertEqual(paid["attempt"].status, PaymentAttempt.Status.PROCESSING)

    def test_typed_orphan_is_selected_and_concrete_expiry_precedes_legacy_null(self):
        legacy = self._graph(
            invoice_expires_at=None,
            age=timedelta(hours=25),
        )
        concrete = self._graph(
            invoice_expires_at=self.now - timedelta(seconds=1),
            age=timedelta(minutes=1),
        )
        orphan = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"typed-orphan-expiry").hexdigest(),
            full_name="Orphan Buyer",
            phone="+380501112244",
            city="Kyiv",
            np_office="Branch 2",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            invoice_expires_at=self.now - timedelta(seconds=1),
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        first = expire_due_assisted_attempts(now=self.now, limit=1)
        concrete["attempt"].refresh_from_db()
        legacy["attempt"].refresh_from_db()
        self.assertEqual(first["expired_attempts"], 1)
        self.assertEqual(concrete["attempt"].status, PaymentAttempt.Status.EXPIRED)
        self.assertEqual(legacy["attempt"].status, PaymentAttempt.Status.PROCESSING)

        expire_due_assisted_attempts(now=self.now, limit=10)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, PaymentAttempt.Status.EXPIRED)
        self.assertEqual(
            orphan.event_state["local_terminalization"]["source"],
            "system_expiry",
        )

    def test_same_digest_starts_fresh_deal_without_mutating_historical_invoice(self):
        graph = self._graph(invoice_expires_at=self.now)
        old_invoice = graph["attempt"].monobank_invoice_id

        from management.services.ig_checkout import create_or_update_proposal
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        replacement = create_or_update_proposal(
            client=graph["client"],
            pay_type="online_full",
            item_specs=[
                {
                    "product_id": self.product.pk,
                    "color_variant_id": self.variant.pk,
                    "qty": 1,
                    "size": "S",
                    "fit_option_code": self.fit.code,
                }
            ],
        )

        graph["attempt"].refresh_from_db()
        graph["proposal"].refresh_from_db()
        graph["deal"].refresh_from_db()
        self.assertNotEqual(replacement.pk, graph["proposal"].pk)
        self.assertNotEqual(replacement.deal_id, graph["deal"].pk)
        self.assertEqual(graph["attempt"].monobank_invoice_id, old_invoice)
        self.assertEqual(graph["proposal"].payment_attempt_id, graph["attempt"].pk)
        self.assertIsNone(graph["deal"].active_checkout_proposal_id)
        replacement.deal.refresh_from_db()
        self.assertEqual(replacement.deal.active_checkout_proposal_id, replacement.pk)

    def test_session_reset_failure_retains_browser_payment_identity(self):
        graph = self._graph(
            invoice_expires_at=self.now + timedelta(minutes=20),
        )
        request = RequestFactory().get("/cart/")
        SessionMiddleware(lambda _request: None).process_request(request)
        request.session["monobank_pending_attempt_id"] = graph["attempt"].pk
        request.session["monobank_attempt_id"] = graph["attempt"].pk
        request.session["monobank_invoice_id"] = graph["attempt"].monobank_invoice_id
        request.session.save()

        from storefront.views.utils import _reset_monobank_session

        with patch(
            "management.services.ig_checkout_terminalization.terminalize_payment_attempt",
            side_effect=DatabaseError("db unavailable"),
        ):
            _reset_monobank_session(request, drop_pending=True)

        self.assertEqual(
            request.session["monobank_pending_attempt_id"], graph["attempt"].pk
        )
        self.assertEqual(request.session["monobank_attempt_id"], graph["attempt"].pk)
        self.assertEqual(
            request.session["monobank_invoice_id"], graph["attempt"].monobank_invoice_id
        )

    def test_mariadb_deadlock_retry_is_bounded(self):
        from management.services.ig_checkout_terminalization import (
            AttemptTerminalizationResult,
            terminalize_payment_attempt,
        )

        expected = AttemptTerminalizationResult(99, "terminalized")
        with patch(
            "management.services.ig_checkout_terminalization._terminalize_payment_attempt_once",
            side_effect=[DatabaseError(1213, "deadlock"), expected],
        ) as terminalize_once, patch(
            "management.services.ig_checkout_terminalization.time.sleep"
        ) as wait:
            result = terminalize_payment_attempt(
                99,
                terminal_status=PaymentAttempt.Status.EXPIRED,
                reason="invoice_expired",
                source="system_expiry",
                now=self.now,
                require_due=True,
            )

        self.assertEqual(result, expected)
        self.assertEqual(terminalize_once.call_count, 2)
        wait.assert_called_once()

    def test_due_selector_dry_run_is_one_bounded_query(self):
        self._graph(invoice_expires_at=self.now)
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        with CaptureQueriesContext(connection) as queries:
            result = expire_due_assisted_attempts(
                now=self.now,
                limit=10,
                dry_run=True,
            )

        self.assertEqual(result["expired_attempts"], 1)
        self.assertLessEqual(len(queries), 1)

    def test_manual_command_isolates_one_row_failure(self):
        first = self._graph(invoice_expires_at=self.now)
        second = self._graph(invoice_expires_at=self.now)
        from management.services.ig_checkout_terminalization import (
            AttemptTerminalizationResult,
        )

        stdout = StringIO()
        stderr = StringIO()
        with patch(
            "orders.management.commands.expire_payment_attempts.terminalize_payment_attempt",
            side_effect=[
                DatabaseError("first failed"),
                AttemptTerminalizationResult(second["attempt"].pk, "terminalized"),
            ],
        ):
            call_command(
                "expire_payment_attempts",
                limit=10,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertIn(str(first["attempt"].pk), stderr.getvalue())
        self.assertIn("Expired 1 payment attempts; errors=1", stdout.getvalue())

    def test_provider_recheck_migration_resumes_partial_mariadb_ddl(self):
        migration = importlib.import_module(
            "orders.migrations.0057_paymentattempt_provider_recheck"
        )
        schema_editor = Mock()
        schema_editor.connection.vendor = "mysql"
        schema_editor.quote_name.side_effect = lambda value: f"`{value}`"
        cursor_context = MagicMock()
        cursor = Mock()
        cursor_context.__enter__.return_value = cursor
        schema_editor.connection.cursor.return_value = cursor_context
        schema_editor.connection.introspection.get_field_type.side_effect = (
            lambda type_code, _column: type_code
        )

        def column(name):
            expected_type, null_ok, size, default = migration.EXPECTED_COLUMNS[name]
            type_code = expected_type[0] if isinstance(expected_type, tuple) else expected_type
            return SimpleNamespace(
                name=name,
                type_code=type_code,
                null_ok=null_ok,
                internal_size=size,
                # MariaDB 11.4 returns SQL NULL text for nullable defaults.
                default="NULL" if default is None else default,
            )

        existing_columns = {
            "provider_recheck_state",
            "provider_recheck_next_at",
            "provider_recheck_until",
        }
        schema_editor.connection.introspection.get_table_description.return_value = [
            column(name) for name in existing_columns
        ]
        schema_editor.connection.introspection.get_constraints.return_value = {
            "pay_attempt_recheck_due": {
                "index": True,
                "unique": False,
                "columns": list(migration.INDEXES["pay_attempt_recheck_due"]),
            }
        }

        migration.ensure_provider_recheck_schema(None, schema_editor)

        statements = [call.args[0] for call in schema_editor.execute.call_args_list]
        self.assertFalse(any("provider_recheck_state" in sql for sql in statements))
        self.assertTrue(any("provider_recheck_claim_token" in sql for sql in statements))
        self.assertTrue(any("pay_attempt_recheck_lease" in sql for sql in statements))
        self.assertFalse(any("pay_attempt_recheck_due" in sql for sql in statements))

        schema_editor.execute.reset_mock()
        schema_editor.connection.introspection.get_table_description.return_value = [
            column(name) for name in migration.COLUMNS
        ]
        schema_editor.connection.introspection.get_constraints.return_value = {
            name: {
                "index": True,
                "unique": False,
                "columns": list(fields),
            }
            for name, fields in migration.INDEXES.items()
        }
        migration.ensure_provider_recheck_schema(None, schema_editor)
        schema_editor.execute.assert_not_called()

        incompatible = column("provider_recheck_state")
        incompatible.internal_size = 32
        schema_editor.connection.introspection.get_table_description.return_value = [
            incompatible
        ]
        with self.assertRaises(RuntimeError):
            migration.ensure_provider_recheck_schema(None, schema_editor)

        schema_editor.connection.introspection.get_table_description.return_value = [
            column(name) for name in migration.COLUMNS
        ]
        schema_editor.connection.introspection.get_constraints.return_value = {
            "pay_attempt_recheck_due": {
                "index": True,
                "unique": False,
                "columns": ["provider_recheck_next_at", "id"],
            }
        }
        with self.assertRaises(RuntimeError):
            migration.ensure_provider_recheck_schema(None, schema_editor)

        from django.db.migrations.exceptions import IrreversibleError

        with self.assertRaises(IrreversibleError):
            migration.refuse_reverse(None, schema_editor)

    def test_late_verified_success_is_accepted_once_and_routes_to_inventory_review(self):
        graph = self._graph(
            invoice_expires_at=self.now,
            with_inventory=True,
        )

        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        expire_due_assisted_attempts(now=self.now, limit=10)
        graph["attempt"].refresh_from_db()

        payload = {
            "status": "success",
            "invoiceId": graph["attempt"].monobank_invoice_id,
            "reference": graph["attempt"].reference,
            "ccy": 980,
            "paidAmount": 90000,
        }
        first_order, first_created = _apply_payment_attempt_status(
            graph["attempt"],
            "success",
            payload=payload,
            source="provider_pull",
        )
        second_order, second_created = _apply_payment_attempt_status(
            graph["attempt"],
            "success",
            payload=payload,
            source="provider_pull",
        )

        graph["attempt"].refresh_from_db()
        graph["proposal"].refresh_from_db()
        graph["reservation"].refresh_from_db()
        self.assertIsNone(first_order)
        self.assertIsNone(second_order)
        self.assertFalse(first_created)
        self.assertFalse(second_created)
        self.assertFalse(Order.objects.exists())
        self.assertIsNone(graph["attempt"].order_id)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.PAID)
        self.assertEqual(
            graph["attempt"].event_state["local_terminalization"][
                "provider_check_state"
            ],
            "resolved",
        )
        self.assertEqual(graph["proposal"].status, IgCheckoutProposal.Status.MANAGER_REVIEW)
        self.assertEqual(
            graph["reservation"].state,
            IgCheckoutInventoryReservation.State.OVERBOOKED_REVIEW,
        )
        self.assertEqual(
            IgPaymentEvent.objects.filter(
                deal=graph["deal"],
                provider_status="success",
            ).count(),
            1,
        )
        from management.models import IgFollowUpTask

        self.assertEqual(
            IgFollowUpTask.objects.filter(
                event_key=f"late-local-payment-review:{graph['attempt'].pk}"
            ).count(),
            1,
        )

    def test_late_verified_success_with_stale_hold_preserves_money_without_consumption(self):
        graph = self._graph(invoice_expires_at=self.now, with_inventory=True)
        # A historical/unbound identity must not be adopted as the current
        # revision's paid allocation, even though its proposal still matches.
        reservation = graph["reservation"]
        reservation.reservation_fingerprint = hashlib.sha256(b"stale-reservation").hexdigest()
        reservation.save(update_fields=["reservation_fingerprint", "updated_at"])
        original_stock = self.variant.stock

        from management.services.ig_checkout_terminalization import expire_due_assisted_attempts
        from management.models import IgFollowUpTask
        from storefront.views.monobank import _apply_payment_attempt_status

        expire_due_assisted_attempts(now=self.now, limit=10)
        payload = {
            "status": "success",
            "invoiceId": graph["attempt"].monobank_invoice_id,
            "reference": graph["attempt"].reference,
            "ccy": 980,
            "paidAmount": 90000,
        }
        for _ in range(2):
            order, created = _apply_payment_attempt_status(
                graph["attempt"], "success", payload=payload, source="provider_pull",
            )
            self.assertIsNone(order)
            self.assertFalse(created)

        for row in (graph["attempt"], graph["proposal"], graph["deal"], reservation, self.variant):
            row.refresh_from_db()
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.PAID)
        self.assertEqual(graph["attempt"].paid_amount, Decimal("900.00"))
        self.assertEqual(graph["deal"].payment_truth, IgDeal.PaymentTruth.CONFIRMED)
        projection = IgPaymentProjection.objects.get(deal=graph["deal"])
        self.assertEqual(projection.truth, IgDeal.PaymentTruth.CONFIRMED)
        self.assertEqual(projection.gross_amount, Decimal("900.00"))
        self.assertTrue(projection.needs_reconciliation)
        self.assertEqual(graph["proposal"].status, IgCheckoutProposal.Status.MANAGER_REVIEW)
        self.assertEqual(reservation.state, IgCheckoutInventoryReservation.State.RELEASED)
        review = IgFollowUpTask.objects.get(event_key=f"inventory-overbooked:{graph['proposal'].pk}")
        self.assertEqual(review.event_payload["inventory_reason"], "inventory_current_revision_hold_missing")
        self.assertEqual(review.event_payload["reservation_ids"], [])
        self.assertEqual(review.kind, IgFollowUpTask.Kind.MANAGER_TASK)
        self.assertFalse(Order.objects.exists())
        self.assertIsNone(graph["attempt"].order_id)
        self.assertIsNone(reservation.order_id)
        self.assertEqual(reservation.fulfilled_quantity, 0)
        self.assertEqual(self.variant.stock, original_stock)
        self.assertFalse(IgCheckoutInventoryReservation.objects.filter(proposal=graph["proposal"], state__in=[
            IgCheckoutInventoryReservation.State.PAID_COMMITTED,
            IgCheckoutInventoryReservation.State.CONSUMED,
            IgCheckoutInventoryReservation.State.FULFILLED,
        ]).exists())
        self.assertEqual(IgPaymentEvent.objects.filter(deal=graph["deal"], provider_status="success").count(), 1)
        self.assertEqual(IgFollowUpTask.objects.filter(
            event_key=f"late-local-payment-review:{graph['attempt'].pk}").count(), 1)

    def test_authenticated_promo_late_success_never_consumes_reissued_capacity(self):
        graph = self._graph(
            invoice_expires_at=self.now,
            with_promo=True,
        )
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        expire_due_assisted_attempts(now=self.now, limit=10)
        graph["promo"].refresh_from_db()
        self.assertEqual(graph["promo"].current_uses, 0)
        from orders.promo_reservations import reserve_promo_for_checkout

        reissued = reserve_promo_for_checkout(
            code=graph["promo"].code,
            user=self.user,
            total_amount=Decimal("900.00"),
        )
        second_attempt = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"promo-reissued-second-attempt").hexdigest(),
            user=self.user,
            full_name="Second Promo Buyer",
            phone="+380501112266",
            city="Kyiv",
            np_office="Branch 4",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("900.00"),
            discount_amount=reissued.discount,
            payable_amount=Decimal("900.00") - reissued.discount,
            payment_amount=Decimal("900.00") - reissued.discount,
            promo_code=graph["promo"],
            event_state=reissued.event_state,
        )
        self.assertTrue(
            second_attempt.event_state["promo_reservation"]["reservation_generation"]
        )
        _apply_payment_attempt_status(
            graph["attempt"],
            "success",
            payload={
                "status": "success",
                "invoiceId": graph["attempt"].monobank_invoice_id,
                "reference": graph["attempt"].reference,
                "ccy": 980,
                "paidAmount": 90000,
            },
            source="provider_pull",
        )

        graph["attempt"].refresh_from_db()
        graph["promo"].refresh_from_db()
        second_attempt.refresh_from_db()
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.PAID)
        self.assertIsNone(graph["attempt"].order_id)
        self.assertEqual(graph["promo"].current_uses, 1)
        self.assertEqual(
            second_attempt.event_state["promo_reservation"]["state"],
            "reserved",
        )
        self.assertFalse(PromoCodeUsage.objects.filter(promo_code=graph["promo"]).exists())

    def test_reconciler_polls_locally_expired_invoice_without_changing_truth(self):
        graph = self._graph(invoice_expires_at=self.now)
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status",
            return_value=("processing", {"status": "processing"}),
        ) as provider:
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        graph["attempt"].refresh_from_db()
        graph["deal"].refresh_from_db()
        provider.assert_called_once()
        self.assertEqual(result["late_status_checked"], 1)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.EXPIRED)
        self.assertEqual(graph["deal"].payment_truth, IgDeal.PaymentTruth.UNVERIFIED)
        self.assertEqual(
            graph["attempt"].event_state["local_terminalization"][
                "provider_check_attempts"
            ],
            1,
        )

    def test_reconciler_late_success_routes_to_review_without_order(self):
        graph = self._graph(invoice_expires_at=self.now)
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        payload = {
            "status": "success",
            "invoiceId": graph["attempt"].monobank_invoice_id,
            "reference": graph["attempt"].reference,
            "ccy": 980,
            "paidAmount": 90000,
        }
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status",
            return_value=("success", payload),
        ):
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        graph["attempt"].refresh_from_db()
        graph["proposal"].refresh_from_db()
        self.assertEqual(result["late_status_checked"], 1)
        self.assertEqual(graph["attempt"].status, PaymentAttempt.Status.PAID)
        self.assertIsNone(graph["attempt"].order_id)
        self.assertEqual(graph["proposal"].status, IgCheckoutProposal.Status.MANAGER_REVIEW)

    def test_expired_backstop_escalates_without_provider_call(self):
        graph = self._graph(invoice_expires_at=self.now)
        from management.models import IgFollowUpTask
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        graph["attempt"].refresh_from_db()
        state = dict(graph["attempt"].event_state)
        local = dict(state["local_terminalization"])
        local["provider_check_until"] = (self.now - timedelta(seconds=1)).isoformat()
        local["provider_next_check_at"] = (self.now - timedelta(seconds=1)).isoformat()
        state["local_terminalization"] = local
        graph["attempt"].event_state = state
        graph["attempt"].provider_recheck_until = self.now - timedelta(seconds=1)
        graph["attempt"].provider_recheck_next_at = self.now - timedelta(seconds=1)
        graph["attempt"].save(update_fields=[
            "event_state", "provider_recheck_until", "provider_recheck_next_at",
            "updated",
        ])

        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status"
        ) as provider:
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        provider.assert_not_called()
        graph["attempt"].refresh_from_db()
        self.assertEqual(result["late_status_exhausted"], 1)
        self.assertEqual(
            graph["attempt"].event_state["local_terminalization"][
                "provider_check_state"
            ],
            "exhausted",
        )
        self.assertTrue(
            IgFollowUpTask.objects.filter(
                event_key=f"local-invoice-status-review:{graph['attempt'].pk}"
            ).exists()
        )

    def test_typed_orphan_backstop_records_success_without_order(self):
        orphan = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"typed-orphan-backstop").hexdigest(),
            full_name="Orphan Backstop",
            phone="+380501112277",
            city="Kyiv",
            np_office="Branch 5",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            monobank_invoice_id="typed-orphan-backstop-invoice",
            invoice_expires_at=self.now,
        )
        from management.models import IgBotNotification
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        payload = {
            "status": "success",
            "invoiceId": orphan.monobank_invoice_id,
            "reference": orphan.reference,
            "ccy": 980,
            "paidAmount": 90000,
        }
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status",
            return_value=("success", payload),
        ):
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        orphan.refresh_from_db()
        self.assertEqual(result["late_status_resolved"], 1)
        self.assertEqual(orphan.status, PaymentAttempt.Status.PAID)
        self.assertIsNone(orphan.order_id)
        self.assertEqual(
            orphan.provider_recheck_state,
            PaymentAttempt.ProviderRecheckState.RESOLVED,
        )
        self.assertEqual(
            orphan.event_state["orphan_provider_review"]["provider_status"],
            "success",
        )
        notification = IgBotNotification.objects.get(
            dedupe_key=f"orphan-provider-payment-review:{orphan.pk}:success"
        )
        self.assertEqual(notification.status, IgBotNotification.Status.PENDING)
        self.assertEqual(notification.payload["attempt_id"], orphan.pk)
        self.assertEqual(notification.payload["invoice_id"], orphan.monobank_invoice_id)
        self.assertEqual(notification.payload["paid_amount"], "900.00")
        from management.services.instagram_bot import drain_manager_notifications

        with patch(
            "management.services.instagram_bot._deliver_manager_notification",
            return_value=True,
        ) as deliver:
            drain_manager_notifications(limit=1)
        deliver.assert_called_with(notification.dedupe_key)

        # Replayed provider truth reuses the same actionable row.
        from storefront.views.monobank import _apply_payment_attempt_status

        _apply_payment_attempt_status(
            orphan,
            "success",
            payload=payload,
            source="provider_pull",
        )
        self.assertEqual(
            IgBotNotification.objects.filter(
                dedupe_key=notification.dedupe_key
            ).count(),
            1,
        )

    def test_provider_apply_crash_restores_pending_instead_of_resolving(self):
        graph = self._graph(invoice_expires_at=self.now)
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        payload = {
            "status": "success",
            "invoiceId": graph["attempt"].monobank_invoice_id,
            "reference": graph["attempt"].reference,
            "ccy": 980,
            "paidAmount": 90000,
        }
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status",
            return_value=("success", payload),
        ), patch(
            "storefront.views.monobank._apply_payment_attempt_status",
            side_effect=RuntimeError("crash between check and apply"),
        ), patch(
            "management.services.ig_checkout_terminalization.logger.exception"
        ):
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        graph["attempt"].refresh_from_db()
        self.assertEqual(result["late_status_errors"], 1)
        self.assertEqual(
            graph["attempt"].provider_recheck_state,
            PaymentAttempt.ProviderRecheckState.PENDING,
        )
        self.assertFalse(IgPaymentEvent.objects.filter(deal=graph["deal"]).exists())

    def test_orphan_notification_failure_prevents_resolved_state(self):
        orphan = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"orphan-notification-failure").hexdigest(),
            full_name="Orphan Notification",
            phone="+380501112300",
            city="Kyiv",
            np_office="Branch 8",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            monobank_invoice_id="orphan-notification-failure-invoice",
            invoice_expires_at=self.now,
        )
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        payload = {
            "status": "success",
            "invoiceId": orphan.monobank_invoice_id,
            "reference": orphan.reference,
            "ccy": 980,
            "paidAmount": 90000,
        }
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status",
            return_value=("success", payload),
        ), patch(
            "management.models.IgBotNotification.objects.get_or_create",
            side_effect=DatabaseError("outbox unavailable"),
        ), patch(
            "management.services.ig_checkout_terminalization.logger.exception"
        ):
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        orphan.refresh_from_db()
        self.assertEqual(result["late_status_errors"], 1)
        self.assertEqual(
            orphan.provider_recheck_state,
            PaymentAttempt.ProviderRecheckState.PENDING,
        )

    def test_typed_orphan_exhaustion_creates_global_actionable_notification(self):
        orphan = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"typed-orphan-exhausted").hexdigest(),
            full_name="Orphan Exhausted",
            phone="+380501112299",
            city="Kyiv",
            np_office="Branch 7",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            monobank_invoice_id="typed-orphan-exhausted-invoice",
            invoice_expires_at=self.now,
        )
        from management.models import IgBotNotification
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        PaymentAttempt.objects.filter(pk=orphan.pk).update(
            provider_recheck_until=self.now - timedelta(seconds=1),
            provider_recheck_next_at=self.now - timedelta(seconds=1),
        )
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status"
        ) as provider:
            result = reconcile_ig_checkout(limit=10, pull_ambiguous=True)

        provider.assert_not_called()
        orphan.refresh_from_db()
        self.assertEqual(result["late_status_exhausted"], 1)
        self.assertEqual(
            orphan.provider_recheck_state,
            PaymentAttempt.ProviderRecheckState.EXHAUSTED,
        )
        notification = IgBotNotification.objects.get(
            dedupe_key=f"orphan-provider-recheck-exhausted:{orphan.pk}"
        )
        self.assertIsNone(notification.client_id)
        self.assertEqual(notification.payload["attempt_id"], orphan.pk)
        self.assertEqual(
            orphan.event_state["orphan_provider_review"]["notification_id"],
            notification.pk,
        )

    def test_not_yet_due_rechecks_do_not_consume_bounded_page(self):
        future_one = self._graph(invoice_expires_at=self.now)
        future_two = self._graph(invoice_expires_at=self.now)
        due = self._graph(invoice_expires_at=self.now)
        from management.services.ig_checkout_reconciliation import reconcile_ig_checkout
        from management.services.ig_checkout_terminalization import (
            expire_due_assisted_attempts,
        )

        expire_due_assisted_attempts(now=self.now, limit=10)
        PaymentAttempt.objects.filter(
            pk__in=[future_one["attempt"].pk, future_two["attempt"].pk]
        ).update(provider_recheck_next_at=self.now + timedelta(hours=1))
        with patch(
            "storefront.views.monobank._resolve_attempt_invoice_status",
            return_value=("processing", {"status": "processing"}),
        ) as provider:
            result = reconcile_ig_checkout(limit=1, pull_ambiguous=True)

        self.assertEqual(result["late_status_due"], 1)
        checked_attempt = provider.call_args.args[0]
        self.assertEqual(checked_attempt.pk, due["attempt"].pk)


@skipUnless(
    connection.features.has_select_for_update,
    "requires MariaDB row-level SELECT FOR UPDATE",
)
class AssistedTerminalizationConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def _fixture_teardown(self):
        # Production append-only triggers reject Django's default DELETE flush.
        for db_name in self._databases_names(include_mirrors=False):
            call_command(
                "flush",
                verbosity=0,
                interactive=False,
                database=db_name,
                reset_sequences=True,
                allow_cascade=self.available_apps is not None,
                inhibit_post_migrate=True,
            )

    def test_two_workers_share_one_terminal_result_without_deadlock(self):
        client = IgClient.get_or_create_for_sender("terminalization-concurrency")
        deal = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.AWAITING_PAYMENT,
            amount=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
        )
        episode = ensure_episode_for_deal(deal)
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            commercial_episode=episode,
            catalog_total=Decimal("900.00"),
            quoted_total=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
            items_digest="c" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        attempt = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"terminalization-concurrency").hexdigest(),
            full_name="Concurrent Buyer",
            phone="+380501112255",
            city="Kyiv",
            np_office="Branch 3",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={"checkout_surface": "instagram_proposal", "cart": []},
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            invoice_expires_at=timezone.now() - timedelta(seconds=1),
        )
        proposal.payment_attempt = attempt
        proposal.status = IgCheckoutProposal.Status.INVOICE_CREATED
        proposal.save(update_fields=["payment_attempt", "status", "updated_at"])

        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )

        barrier = threading.Barrier(2)
        outcomes = []
        errors = []

        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                result = terminalize_payment_attempt(
                    attempt.pk,
                    terminal_status=PaymentAttempt.Status.EXPIRED,
                    reason="invoice_expired",
                    source="system_expiry",
                    require_due=True,
                )
                outcomes.append(result.outcome)
            except Exception as exc:  # pragma: no cover - MariaDB diagnostic
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertCountEqual(outcomes, ["terminalized", "already_terminal"])
        attempt.refresh_from_db()
        deal.refresh_from_db()
        self.assertEqual(attempt.status, PaymentAttempt.Status.EXPIRED)
        self.assertIsNone(deal.active_checkout_proposal_id)

    def test_terminalizer_races_provider_projection_without_duplicate_order(self):
        client = IgClient.get_or_create_for_sender("terminal-provider-race")
        deal = IgDeal.objects.create(
            client=client,
            status=IgDeal.Status.AWAITING_PAYMENT,
            amount=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
        )
        episode = ensure_episode_for_deal(deal)
        proposal = IgCheckoutProposal.objects.create_current(
            deal=deal,
            commercial_episode=episode,
            catalog_total=Decimal("900.00"),
            quoted_total=Decimal("900.00"),
            requested_payment_amount=Decimal("900.00"),
            items_digest="d" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        attempt = PaymentAttempt.objects.create(
            fingerprint=hashlib.sha256(b"terminal-provider-race").hexdigest(),
            full_name="Race Buyer",
            phone="+380501112288",
            city="Kyiv",
            np_office="Branch 6",
            pay_type=PaymentAttempt.PayType.ONLINE_FULL,
            status=PaymentAttempt.Status.PROCESSING,
            cart_snapshot={
                "checkout_surface": "instagram_proposal",
                "sale_source": "Instagram",
                "cart": [],
            },
            gross_amount=Decimal("900.00"),
            payable_amount=Decimal("900.00"),
            payment_amount=Decimal("900.00"),
            monobank_invoice_id="terminal-provider-race-invoice",
            invoice_expires_at=timezone.now() - timedelta(seconds=1),
        )
        proposal.payment_attempt = attempt
        proposal.status = IgCheckoutProposal.Status.INVOICE_CREATED
        proposal.save(update_fields=["payment_attempt", "status", "updated_at"])

        from management.services.ig_checkout_terminalization import (
            terminalize_payment_attempt,
        )
        from storefront.views.monobank import _apply_payment_attempt_status

        barrier = threading.Barrier(2)
        errors = []
        payload = {
            "status": "success",
            "invoiceId": attempt.monobank_invoice_id,
            "reference": attempt.reference,
            "ccy": 980,
            "paidAmount": 90000,
        }

        def expire_worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                terminalize_payment_attempt(
                    attempt.pk,
                    terminal_status=PaymentAttempt.Status.EXPIRED,
                    reason="invoice_expired",
                    source="system_expiry",
                    require_due=True,
                )
            except Exception as exc:  # pragma: no cover - MariaDB diagnostic
                errors.append(exc)
            finally:
                close_old_connections()

        def provider_worker():
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                _apply_payment_attempt_status(
                    attempt,
                    "success",
                    payload=payload,
                    source="provider_pull",
                )
            except Exception as exc:  # pragma: no cover - MariaDB diagnostic
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [
            threading.Thread(target=expire_worker),
            threading.Thread(target=provider_worker),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertLessEqual(Order.objects.count(), 1)
        attempt.refresh_from_db()
        self.assertIn(
            attempt.status,
            {
                PaymentAttempt.Status.EXPIRED,
                PaymentAttempt.Status.PAID,
                PaymentAttempt.Status.CONVERTED,
            },
        )
