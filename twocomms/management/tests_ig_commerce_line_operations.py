"""Canonical positions are source-bound draft facts, not physical order counts."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from unittest import skipUnless
from threading import Barrier, Event, Thread
from queue import Queue
from types import SimpleNamespace
import os
import json
import hashlib

from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from management.models import (IgClient, IgCommercialEpisode, IgCommerceSelectionSession,
    IgCommerceSelectionTransition, IgCommerceTurnDecision, IgFunnelResetAudit,
    InstagramBotMessage, InstagramBotSettings)
from management.services.ig_commerce_projection import capture_current_selection_lines
from management.services.ig_commerce_state import apply_turn, CommerceRevisionConflict, _apply_line_operations
from management.services.ig_commerce_turns import parse_turn
from management.services.ig_commerce_types import CommerceLineOperation, CommerceTurnRequest
from management.services.instagram_bot import ingress_provider_namespace
from orders.models import Order


class CommerceLineOperationBatchTests(SimpleTestCase):
    def before(self):
        return {'lines': [{'line_id': 'line:1:0', 'product_id': 101, 'garment_type': 'tshirt',
            'color': 'black', 'size': 'L', 'paid_amount': '790', 'quantity': 1},
            {'line_id': 'line:2:0', 'product_id': 202, 'garment_type': 'tshirt', 'size': 'M'}],
            'active_index': 1, 'query_constraints': {}, 'revision': 2}

    def test_exact_old_product_selector_cannot_replace_other_active_same_garment(self):
        before = self.before()
        request = CommerceTurnRequest(line_operations=(CommerceLineOperation('replace',
            target_product_id=101, exact_product_id=303, garment_type='hoodie'),))
        after, _, reason = _apply_line_operations(before, request, SimpleNamespace(pk=3))
        self.assertEqual(reason, '')
        self.assertEqual(after['lines'][0]['product_id'], 303)
        self.assertEqual(after['lines'][1], before['lines'][1])
        self.assertNotIn('size', after['lines'][0])
        self.assertNotIn('paid_amount', after['lines'][0])
        self.assertEqual(before['lines'][0]['product_id'], 101)

    def test_failed_second_operation_rolls_back_entire_pure_batch(self):
        before = self.before()
        original = deepcopy(before)
        request = CommerceTurnRequest(line_operations=(CommerceLineOperation('add', garment_type='hoodie'),
            CommerceLineOperation('update', target_product_id=999, field_updates={'color': 'pink'})))
        after, facts, reason = _apply_line_operations(before, request, SimpleNamespace(pk=3))
        self.assertIsNone(after)
        self.assertEqual(facts, [])
        self.assertEqual(reason, 'missing_line_target')
        self.assertEqual(before, original)


@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CommerceLineOperationsTests(TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'IG_PROVIDER_TRANSPORT': 'instagram_login'})
        environment.start()
        self.addCleanup(environment.stop)
        self.customer = IgClient.objects.create(igsid='line-ops-' + self._testMethodName[:45])
        settings = InstagramBotSettings.objects.create(pk=1, ig_user_id='cart-source-owner')
        self.namespace = ingress_provider_namespace(settings)
        self.now = timezone.now()
        self.ordinal = 0

    def source(self, text, *, at=None):
        self.ordinal += 1
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role='user', source='webhook', text=text, mid=f'cart-source-{self.ordinal}',
            provider_namespace=self.namespace,
            provider_created_at=at or self.now - timedelta(minutes=5) + timedelta(seconds=self.ordinal))

    def reduce(self, text, *, at=None, expected_revision=None):
        source = self.source(text, at=at)
        request = parse_turn(text)
        decision = apply_turn(self.customer, source, request, expected_revision=expected_revision, reply_payload={})
        self.customer.refresh_from_db()
        decision.session.refresh_from_db()
        return source, decision

    def capture(self):
        return capture_current_selection_lines(self.customer.pk, now=self.now)

    def initial(self):
        return self.reduce('добавьте чёрную футболку размер L')

    def reduce_old_recipient_proof(self, text):
        from management.services import ig_commerce_state as reducer
        create = reducer._create_decision
        def old_producer(*args, **kwargs):
            payload = deepcopy(kwargs['result_payload'])
            for row in payload['line_source_facts']['operations']:
                row.pop('target_recipient_id', None)
            return create(*args, **{**kwargs, 'result_payload': payload})
        # Emulate the older producer at INSERT. Immutable model/database
        # guards remain active and no accepted decision is rewritten.
        with patch.object(reducer, '_create_decision', side_effect=old_producer):
            return self.reduce(text)

    def test_old_v1_empty_recipient_selector_keeps_existing_source_proof_without_rewriting_it(self):
        source, decision = self.reduce_old_recipient_proof('добавьте чёрную футболку размер L')
        stored = deepcopy(decision.result_payload)
        self.assertNotIn('target_recipient_id', stored['line_source_facts']['operations'][0])
        captured = self.capture()
        self.assertTrue(captured['coverage_complete'], captured)
        self.assertEqual(captured['lines'][0]['fields']['size']['value'], 'L')
        self.assertEqual(captured['lines'][0]['fields']['color']['value'], 'black')
        self.assertEqual(captured['lines'][0]['evidence']['size']['source_message_id'], source.pk)
        decision.refresh_from_db()
        self.assertEqual(decision.result_payload, stored)

    def test_missing_nonempty_recipient_selector_never_becomes_legacy_default_authority(self):
        self.initial()
        self.reduce('добавьте худи для друга размер M')
        _, decision = self.reduce_old_recipient_proof('измените худи для друга на розовый')
        self.assertNotIn('target_recipient_id', decision.result_payload['line_source_facts']['operations'][0])
        captured = self.capture()
        self.assertFalse(captured['coverage_complete'])
        self.assertEqual(captured['lines'][1]['fields'], {})

    def test_reused_initial_owner_snapshot_still_requires_exact_fresh_final_owner(self):
        from management.services.ig_admin_state_capture import _owner_fence
        self.initial()
        first = _owner_fence(self.customer.pk)
        self.assertEqual(capture_current_selection_lines(self.customer.pk, now=self.now, _owner_snapshot=first)['status'], 'captured')
        forged = deepcopy(first)
        forged['owner']['reply_permission_epoch'] += 1
        denied = capture_current_selection_lines(self.customer.pk, now=self.now, _owner_snapshot=forged)
        self.assertEqual((denied['status'], denied['reason']), ('conflict', 'selection_capture_changed'))
        self.assertEqual(first, _owner_fence(self.customer.pk))

    def attach_order(self, *, paid):
        self.customer.refresh_from_db()
        episode = IgCommercialEpisode.objects.get(pk=self.customer.current_commercial_episode_id)
        order = Order.objects.create(full_name='Source owner', phone='380501234567', total_sum=790,
            payment_status='paid' if paid else 'unpaid')
        episode.intended_order = order
        episode.save(update_fields=['intended_order', 'updated_at'])
        return episode, order

    def test_add_preserves_first_position_episode_and_original_sources(self):
        first_source, first = self.initial()
        original = deepcopy(first.session.lines[0])
        episode = self.customer.current_commercial_episode_id
        added_source, added = self.reduce('добавьте худи для друга')
        self.assertEqual(first.session_id, added.session_id)
        self.assertEqual(episode, self.customer.current_commercial_episode_id)
        self.assertEqual(added.session.lines[0], original)
        self.assertEqual(len(added.session.lines), 2)
        capture = self.capture()
        self.assertEqual(capture['status'], 'captured')
        self.assertEqual(capture['lines'][0]['evidence']['color']['source_message_id'], first_source.pk)
        self.assertEqual(capture['lines'][1]['evidence']['garment_type']['source_message_id'], added_source.pk)
        self.assertEqual(capture['lines'][1]['recipient_id'], 'friend')
        self.assertNotIn('size', capture['lines'][1]['fields'])
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.customer).count(), 1)

    def test_named_color_correction_preserves_other_active_line_and_proven_history(self):
        black, first = self.initial()
        self.reduce('добавьте худи для друга')
        pink, changed = self.reduce('не чёрную, а розовую футболку')
        self.assertEqual(changed.session.lines[0]['color'], 'pink')
        self.assertNotIn('color', changed.session.lines[1])
        captured = self.capture()['lines'][0]
        self.assertEqual(captured['evidence']['color']['source_message_id'], pink.pk)
        colors = [row for row in captured['history'] if row['field'] == 'color']
        self.assertEqual([row['value'] for row in colors], ['pink', 'black'])
        self.assertEqual(colors[0]['previous'], 'black')
        self.assertEqual(colors[0]['previous_source']['source_message_id'], black.pk)
        self.assertEqual(colors[1]['superseded_by'], changed.transition_id)

    def test_two_matching_positions_need_clarification_without_mutating_cart(self):
        self.initial()
        _, second = self.reduce('добавьте футболку размер M')
        before = deepcopy(second.session.lines)
        episode = self.customer.current_commercial_episode_id
        _, ambiguous = self.reduce('не чёрную, а розовую футболку')
        self.assertEqual(ambiguous.transition.action, 'line_clarification_required')
        self.assertEqual(ambiguous.session.lines, before)
        self.assertEqual(episode, self.customer.current_commercial_episode_id)
        self.assertEqual(self.capture()['status'], 'captured')

    def test_remove_and_select_preserve_stable_identity_after_array_shift(self):
        _, first = self.initial()
        self.reduce('добавьте худи размер XL')
        _, third = self.reduce('добавьте футболку размер M')
        ids = [line['line_id'] for line in third.session.lines]
        _, removed = self.reduce('уберите первую футболку')
        self.assertEqual([line['line_id'] for line in removed.session.lines], ids[1:])
        _, selected = self.reduce(f'select {ids[2]}')
        self.assertEqual(selected.session.active_index, 1)
        capture = self.capture()
        self.assertEqual(capture['active_line_id'], ids[2])
        self.assertEqual(capture['lines'][0]['fields']['size']['value'], 'XL')

    def test_replace_clears_previous_configuration_and_history_keeps_source_identity(self):
        self.initial()
        _, replaced = self.reduce('вместо футболки худи')
        self.assertEqual(replaced.session.lines[0]['garment_type'], 'hoodie')
        self.assertNotIn('size', replaced.session.lines[0])
        self.assertNotIn('color', replaced.session.lines[0])
        self.assertNotIn('size', self.capture()['lines'][0]['fields'])

    def test_default_quantity_is_not_source_confirmation_but_explicit_quantity_is(self):
        self.initial()
        first = self.capture()['lines'][0]
        self.assertNotIn('quantity', first['fields'])
        self.assertFalse(first['defaults']['quantity']['source_confirmed'])
        source, _ = self.reduce('измените количество футболки на 2')
        first = self.capture()['lines'][0]
        self.assertEqual(first['fields']['quantity']['value'], 2)
        self.assertEqual(first['fields']['quantity']['source']['source_message_id'], source.pk)

    def test_mid_replay_has_no_new_session_episode_transition_or_revision(self):
        source, decision = self.initial()
        counts = (IgCommercialEpisode.objects.count(), IgCommerceSelectionSession.objects.count(),
            IgCommerceSelectionTransition.objects.count())
        replay = apply_turn(self.customer, source, parse_turn(source.text), expected_revision=0)
        self.assertEqual(replay.pk, decision.pk)
        self.assertEqual(counts, (IgCommercialEpisode.objects.count(), IgCommerceSelectionSession.objects.count(),
            IgCommerceSelectionTransition.objects.count()))

    def test_forged_operation_is_rejected_before_bootstrap_or_selection_writes(self):
        source = self.source('добавьте худи')
        request = replace(parse_turn(source.text), line_operations=(CommerceLineOperation('add', garment_type='tshirt'),))
        with self.assertRaisesMessage(CommerceRevisionConflict, 'line_source_unverified'):
            apply_turn(self.customer, source, request)
        self.assertFalse(IgCommercialEpisode.objects.exists())
        self.assertFalse(IgCommerceSelectionSession.objects.exists())
        self.assertFalse(IgCommerceTurnDecision.objects.exists())

    def test_caller_cannot_erase_typed_intent_to_mutate_through_scalar_path(self):
        source = self.source('добавьте худи')
        request = replace(parse_turn(source.text), line_operations=(), field_updates={'size': 'M'})
        with self.assertRaisesMessage(CommerceRevisionConflict, 'line_source_unverified'):
            apply_turn(self.customer, source, request)
        self.assertFalse(IgCommerceSelectionTransition.objects.exists())
        self.assertFalse(IgCommerceSelectionSession.objects.exists())

    def test_foreign_mid_replay_cannot_return_another_clients_decision(self):
        source, decision = self.initial()
        foreign = IgClient.objects.create(igsid='foreign-cart-owner')
        with self.assertRaisesMessage(CommerceRevisionConflict, 'commerce_source_scope_mismatch'):
            apply_turn(foreign, source, parse_turn(source.text))
        self.assertFalse(IgCommerceSelectionSession.objects.filter(client=foreign).exists())
        self.assertEqual(IgCommerceTurnDecision.objects.get(source_message=source).pk, decision.pk)

    def test_stale_event_does_not_replace_cart_or_start_repeat(self):
        _, decision = self.initial()
        before = deepcopy(decision.session.snapshot())
        self.attach_order(paid=True)
        _, stale = self.reduce('ещё одну такую же', at=self.now-timedelta(hours=1))
        self.assertTrue(stale.is_stale)
        decision.session.refresh_from_db()
        self.assertEqual(decision.session.snapshot(), before)
        self.assertEqual(IgCommercialEpisode.objects.count(), 1)

    def test_paid_repeat_uses_original_revision_cas_and_current_copy_intent_receipt(self):
        original, _ = self.initial()
        self.reduce('не чёрную, а розовую футболку')
        old, order = self.attach_order(paid=True)
        source, repeated = self.reduce('ещё одну такую же', expected_revision=2)
        self.assertNotEqual(old.pk, self.customer.current_commercial_episode_id)
        self.assertEqual(repeated.session.revision, 1)
        self.assertIsNone(repeated.session.commercial_episode.intended_order_id)
        line = self.capture()['lines'][0]
        self.assertEqual(line['fields']['size']['source']['source_message_id'], source.pk)
        self.assertEqual(line['fields']['size']['source']['copied_from']['proof']['source_message_id'], original.pk)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

    def test_unpaid_physical_order_add_is_clarification_and_preserves_issued_order(self):
        _, initial = self.initial()
        old, order = self.attach_order(paid=False)
        before = deepcopy(initial.session.lines)
        _, added = self.reduce('добавьте худи')
        self.assertEqual(added.session.lines, before)
        self.assertEqual(added.session.pending_clarification, 'existing_order_amendment_or_new')
        self.assertEqual(self.customer.current_commercial_episode_id, old.pk)
        order.refresh_from_db()
        self.assertEqual(order.total_sum, 790)

    def test_same_again_with_multiple_positions_cannot_infer_most_recent_active(self):
        self.initial()
        _, second = self.reduce('добавьте худи')
        before = deepcopy(second.session.lines)
        _, ambiguous = self.reduce('ещё одну такую же')
        self.assertEqual(ambiguous.session.lines, before)
        self.assertEqual(ambiguous.transition.action, 'line_clarification_required')
        self.assertEqual(IgCommercialEpisode.objects.count(), 1)

    def test_explicit_new_order_can_open_clean_draft_after_unpaid_physical_order(self):
        self.initial()
        old, order = self.attach_order(paid=False)
        _, new = self.reduce('Хочу новый заказ: добавьте худи', expected_revision=1)
        self.assertNotEqual(self.customer.current_commercial_episode_id, old.pk)
        self.assertEqual(len(new.session.lines), 1)
        self.assertEqual(new.session.lines[0]['garment_type'], 'hoodie')
        self.assertNotIn('color', new.session.lines[0])
        self.assertIsNone(new.session.commercial_episode.intended_order_id)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'unpaid')

    def test_source_mutation_and_reset_never_resurrect_old_copy_authority(self):
        original, _ = self.initial()
        self.reduce('ещё одну такую же')
        InstagramBotMessage.objects.filter(pk=original.pk).update(text='раньше хотел худи')
        copied = self.capture()['lines'][1]
        self.assertNotIn('size', copied['fields'])
        IgFunnelResetAudit.objects.create(client=self.customer, reset_after_message_id=original.pk)
        capture = self.capture()
        self.assertTrue(all('size' not in line['fields'] for line in capture['lines']))

    def test_capture_is_bounded_read_only_and_digest_stable_with_fixed_clock(self):
        self.initial()
        self.reduce('добавьте худи')
        with CaptureQueriesContext(connection) as queries:
            first, second = self.capture(), self.capture()
        self.assertEqual(first['capture_digest'], second['capture_digest'])
        self.assertLessEqual(len(queries), 64)
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in queries))
        self.assertEqual(first['transition_limit'], 64)

    def test_source_fence_manifest_includes_supporting_noop_not_only_choice_receipts(self):
        selected, _ = self.initial()
        observed, _ = self.reduce('Гаразд')
        captured = self.capture()
        self.assertEqual(captured['status'], 'captured')
        ids = captured['fence']['source_ids']
        self.assertEqual(ids, sorted({selected.pk, observed.pk}))
        self.assertLessEqual(len(ids), 64)
        self.assertFalse(any(proof['source_message_id'] == observed.pk
            for line in captured['lines'] for proof in line['evidence'].values()))
        from management.services.ig_commerce_projection import _source_fence_row
        frames = {row.pk: _source_fence_row(row) for row in InstagramBotMessage.objects.filter(pk__in=ids)}
        digest = hashlib.sha256(json.dumps(frames, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(digest, captured['fence']['source_digest'])

    def test_final_consumer_rejects_changed_supporting_noop_in_frozen_dto(self):
        self.initial()
        observed, _ = self.reduce('Гаразд')
        captured = self.capture()
        self.assertEqual(captured['status'], 'captured')
        from management.services.ig_turn_capture import validate_current_source_cart_sources
        from management.services.ig_turn_intelligence import TurnContextError
        with CaptureQueriesContext(connection) as first_reads:
            self.assertTrue(validate_current_source_cart_sources(captured))
        self.assertEqual(len(first_reads), 1)
        InstagramBotMessage.objects.filter(pk=observed.pk).update(text='changed supporting observation')
        # Hand the consumer the exact earlier DTO once. Owner/session/MID/event
        # time remain unchanged; only the unseen supporting source changed.
        with patch('management.services.ig_commerce_projection.capture_current_selection_lines',
                side_effect=AssertionError('final source fence must not recapture commerce')):
            with CaptureQueriesContext(connection) as final_reads:
                with self.assertRaises(TurnContextError) as rejected:
                    validate_current_source_cart_sources(captured)
        self.assertEqual(rejected.exception.reason, 'source_cart_sources_changed')
        self.assertEqual(len(final_reads), 1)
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in final_reads))

    def test_final_source_fence_detects_mutation_during_capture(self):
        source, _ = self.initial()
        from management.services import ig_commerce_projection as projection
        original = projection._selection_history
        changed_once = False
        def changed(*args, **kwargs):
            nonlocal changed_once
            result = original(*args, **kwargs)
            # The facade denies writes on its own connection. This injected
            # post-read frame differs from the final exact source observation.
            if not changed_once:
                args[1]['transitions'][0].source_message.text = 'changed while capturing'
                changed_once = True
            return result
        with patch.object(projection, '_selection_history', side_effect=changed):
            result = self.capture()
        self.assertEqual((result['status'], result['reason']), ('conflict', 'selection_capture_changed'))

    def test_read_facade_stops_before_any_accidental_write_or_unbounded_queries(self):
        from management.services import ig_commerce_projection as projection
        def writes(*args, **kwargs):
            IgCommerceSelectionSession.objects.create(client=self.customer, generation=1)
        # ORM save marks its caller transaction for rollback even though the
        # facade refused SQL before execution; isolate that intentional attempt.
        with transaction.atomic():
            with patch.object(projection, '_capture_current_selection_lines', side_effect=writes):
                self.assertEqual(self.capture()['reason'], 'selection_capture_not_read_only')
        self.assertFalse(IgCommerceSelectionSession.objects.exists())
        executed = []
        def too_many(*args, **kwargs):
            def actual(execute, sql, params, many, context):
                executed.append(sql)
                return execute(sql, params, many, context)
            with connection.execute_wrapper(actual):
                for _ in range(65):
                    IgClient.objects.filter(pk=self.customer.pk).exists()
        with CaptureQueriesContext(connection) as queries:
            with patch.object(projection, '_capture_current_selection_lines', side_effect=too_many):
                result = self.capture()
        self.assertEqual(result['reason'], 'selection_capture_query_bound')
        self.assertEqual(len(executed), 64)

    def test_erasure_abstains_without_mutating_any_selection(self):
        self.initial()
        IgClient.objects.filter(pk=self.customer.pk).update(privacy_erasure_started_at=self.now)
        with CaptureQueriesContext(connection) as queries:
            result = self.capture()
        self.assertEqual(result['reason'], 'client_erasing')
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in queries))


@skipUnless(connection.vendor == 'mysql', 'row-lock contention requires native MariaDB')
@override_settings(GOOGLE_INDEXING_ENABLED=False)
class CommerceLineOperationsNativeTests(TransactionTestCase):
    setUp = CommerceLineOperationsTests.setUp
    source = CommerceLineOperationsTests.source
    reduce = CommerceLineOperationsTests.reduce
    initial = CommerceLineOperationsTests.initial
    attach_order = CommerceLineOperationsTests.attach_order

    def race(self, sources, *, expected_revision):
        barrier, outcomes = Barrier(len(sources)), Queue()
        def worker(source_id):
            connections.close_all()
            try:
                source = InstagramBotMessage.objects.get(pk=source_id)
                barrier.wait(timeout=10)
                result = apply_turn(self.customer, source, parse_turn(source.text), expected_revision=expected_revision)
                outcomes.put(('accepted', result.pk))
            except CommerceRevisionConflict as exc:
                outcomes.put(('conflict', str(exc)))
            except Exception as exc:
                outcomes.put(('error', repr(exc)))
            finally:
                connections.close_all()
        threads = [Thread(target=worker, args=(source.pk,), daemon=True) for source in sources]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive(), 'native line-operation race did not settle')
        return [outcomes.get_nowait() for _ in threads]

    def test_concurrent_same_mid_paid_repeat_materializes_once(self):
        self.initial()
        old, _ = self.attach_order(paid=True)
        source = self.source('ещё одну такую же')
        results = self.race([source, source], expected_revision=1)
        self.assertEqual([row[0] for row in results], ['accepted', 'accepted'])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(IgCommercialEpisode.objects.filter(client=self.customer).count(), 2)
        self.assertEqual(IgCommerceSelectionSession.objects.filter(client=self.customer).count(), 2)
        self.assertEqual(IgCommerceTurnDecision.objects.filter(source_message=source).count(), 1)
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(source_message=source).count(), 1)

    def test_concurrent_same_revision_different_updates_has_one_cas_winner(self):
        self.initial()
        pink = self.source('не чёрную, а розовую футболку')
        white = self.source('не чёрную, а белую футболку')
        results = self.race([pink, white], expected_revision=1)
        self.assertEqual(sorted(row[0] for row in results), ['accepted', 'conflict'])
        self.assertEqual(IgCommerceSelectionTransition.objects.filter(session__client=self.customer).count(), 2)
        session = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1)
        self.assertEqual(session.revision, 2)
        self.assertIn(session.lines[0]['color'], ['pink', 'white'])

    def test_erasure_while_waiting_for_client_lock_rejects_without_new_transition(self):
        self.initial()
        source = self.source('добавьте худи')
        waiting, outcome = Event(), Queue()
        def worker():
            connections.close_all()
            def observed(execute, sql, params, many, context):
                if 'management_igclient' in sql and 'FOR UPDATE' in sql.upper():
                    waiting.set()
                return execute(sql, params, many, context)
            try:
                with connections['default'].execute_wrapper(observed):
                    apply_turn(self.customer, source, parse_turn(source.text), expected_revision=1)
                outcome.put('accepted')
            except CommerceRevisionConflict as exc:
                outcome.put(str(exc))
            except Exception as exc:
                outcome.put(repr(exc))
            finally:
                connections.close_all()
        with transaction.atomic():
            locked = IgClient.objects.select_for_update().get(pk=self.customer.pk)
            thread = Thread(target=worker, daemon=True)
            thread.start()
            self.assertTrue(waiting.wait(timeout=10), 'worker never reached canonical client lock')
            locked.privacy_erasure_started_at = self.now
            locked.save(update_fields=['privacy_erasure_started_at'])
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get_nowait(), 'line_source_unverified')
        self.assertFalse(IgCommerceTurnDecision.objects.filter(source_message=source).exists())
        self.assertFalse(IgCommerceSelectionTransition.objects.filter(source_message=source).exists())
