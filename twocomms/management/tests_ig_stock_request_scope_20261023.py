"""Source-owned stock wishes keep their full scope until physical manager review."""
from datetime import timedelta

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from management import tests_ig_restock_waiting_list as waiting_fixtures
from management import tests_ig_revision_live as live
from management import tests_ig_live_conversation_contract_20261022 as contract
from management.models import IgFollowUpTask
from management.services import ig_restock_subscriptions as restock
from product_catalog.models import ProductInventoryPolicy, VariantSizeRule


class UnlistedProductSourceTests(SimpleTestCase):
    def capture(self, text):
        from management.services.ig_product_inquiries import capture_product_inquiries
        return capture_product_inquiries([{'message_id': 7, 'role': 'user', 'text': text, 'source_digest': 'a' * 64}],
            cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
            readiness_by_line={'selected': {'product': {'id': 12}, 'color': {'options': [{'name': 'Black'}]}}})

    def test_quotes_history_negations_and_interface_color_do_not_become_new_wishes(self):
        for text in ('Раніше хотів худі кольору мокко', '«Хочу худі кольору мокко»',
                     'Не хочу худі кольору мокко', 'Мокко колір не хочу', 'Який колір кнопки оплати?'):
            with self.subTest(text=text):
                self.assertFalse(self.capture(text)['inquiries'])

    def test_unlisted_names_are_source_wishes_not_catalogue_or_checkout_facts(self):
        for text, name in (('Хочу худі кольору мокко', 'мокко'), ('Хочу гірчичний колір худі', 'гірчичний'),
                           ('Хочу худі кольору м’ятний', 'м’ятний')):
            with self.subTest(text=text):
                row, = self.capture(text)['inquiries']
                self.assertEqual((row['axis'], row['requested_value']), ('color', name))
                self.assertEqual(row['question'], text)
                self.assertFalse(row['catalog_authority'])
                self.assertFalse(row['checkout_authority'])

    def test_unknown_lining_and_fleece_aliases_remain_exact_questions(self):
        for text in ('Яка підкладка у худі?', 'Какая подкладка у худи?', 'Чи є начіс у худі?',
                     'Есть начес у худи?', 'What lining does this hoodie use?', 'Does this hoodie have fleece?'):
            with self.subTest(text=text):
                row, = self.capture(text)['inquiries']
                self.assertEqual(row['axis'], 'information')
                self.assertEqual(row['question'], text)
                self.assertFalse(row['catalog_authority'])

    def test_later_freeform_color_replaces_the_same_lines_older_wish(self):
        from management.services.ig_product_inquiries import capture_product_inquiries
        disposition = capture_product_inquiries([
            {'message_id': 7, 'role': 'user', 'text': 'Хочу худі кольору мокко'},
            {'message_id': 8, 'role': 'user', 'text': 'Краще худі кольору гірчичний'},
        ], cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
            readiness_by_line={'selected': {'product': {'id': 12}, 'color': {'options': [{'name': 'Black'}]}}})
        row, = disposition['inquiries']
        self.assertEqual((row['axis'], row['requested_value'], row['source_message_id']), ('color', 'гірчичний', 8))

    def test_alternative_questions_quotes_and_negation_do_not_replace_an_earlier_color_wish(self):
        from management.services.ig_product_inquiries import capture_product_inquiries
        for text, expected in (('Чи є худі кольору гірчичний?', ['мокко', 'гірчичний']),
                               ('Не хочу regular, чи є худі кольору гірчичний?', ['мокко', 'гірчичний']),
                               ('«Хочу худі кольору гірчичний»', ['мокко']),
                               ('Не хочу худі кольору гірчичний', ['мокко'])):
            with self.subTest(text=text):
                disposition = capture_product_inquiries([
                    {'message_id': 7, 'role': 'user', 'text': 'Хочу худі кольору мокко'},
                    {'message_id': 8, 'role': 'user', 'text': text},
                ], cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
                    readiness_by_line={'selected': {'product': {'id': 12}, 'color': {'options': [{'name': 'Black'}]}}})
                self.assertEqual([row['requested_value'] for row in disposition['inquiries']], expected)

    def test_supersession_witness_never_discards_material_pickup_or_another_sku(self):
        from management.services.ig_product_inquiries import capture_product_inquiries
        for previous in ('Хочу худі кольору мокко. Який склад?', 'Хочу худі кольору мокко, потрібен самовивіз'):
            with self.subTest(previous=previous):
                disposition = capture_product_inquiries([
                    {'message_id': 7, 'role': 'user', 'text': previous, 'source_digest': 'a' * 64},
                    {'message_id': 8, 'role': 'user', 'text': 'Хочу худі кольору гірчичний', 'source_digest': 'b' * 64},
                ], cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
                    readiness_by_line={'selected': {'product': {'id': 12}, 'color': {'options': [{'name': 'Black'}]}}})
                self.assertFalse(disposition['superseded_color_wishes'])
        disposition = capture_product_inquiries([
            {'message_id': 7, 'role': 'user', 'text': 'Хочу худі кольору мокко', 'source_digest': 'a' * 64},
            {'message_id': 8, 'role': 'user', 'text': 'Хочу худі кольору гірчичний', 'source_digest': 'b' * 64},
        ], cart_capture={'active_line_id': 'second', 'lines': [
            {'line_id': line, 'fields': {'product_id': {'value': pk, 'source': {'source_message_id': source_id}}}}
            for line, pk, source_id in (('first', 12, 7), ('second', 13, 8))]},
            readiness_by_line={line: {'product': {'id': pk}, 'color': {'options': [{'name': 'Black'}]}}
                for line, pk in (('first', 12), ('second', 13))})
        self.assertFalse(disposition['superseded_color_wishes'])
        self.assertEqual({row['product_id'] for row in disposition['inquiries']}, {12, 13})

    def test_supersession_witness_stays_bounded_for_a_long_correction_bundle(self):
        from management.services.ig_product_inquiries import capture_product_inquiries
        sources = [{'message_id': index, 'role': 'user', 'text': f'Хочу худі кольору мокко{letter}',
                    'source_digest': f'{index:064x}'} for index, letter in enumerate('abcdefghijklmnopqrs', 1)]
        disposition = capture_product_inquiries(sources,
            cart_capture={'active_line_id': 'selected', 'lines': [{'line_id': 'selected', 'product_id': 12}]},
            readiness_by_line={'selected': {'product': {'id': 12}, 'color': {'options': [{'name': 'Black'}]}}})
        self.assertEqual(len(disposition['superseded_color_wishes']), 16)
        row, = disposition['inquiries']
        self.assertEqual((row['requested_value'], row['source_message_id']), ('моккоs', 19))

    def test_generic_color_question_does_not_invent_a_color_name(self):
        for text in ('What color is this hoodie?', 'Which color is available?', 'Якого кольору ця футболка?',
                     'Какой цвет у худи?', 'Color please?', 'Які кольори худі є?', 'Покажіть кольори футболки',
                     'What colors do you have for this hoodie?', 'Які є розміри та кольори?'):
            with self.subTest(text=text):
                self.assertFalse(any(row['axis'] == 'color' for row in self.capture(text)['inquiries']))

    def test_each_color_label_retains_only_its_actual_adjacent_value(self):
        from management.services.ig_product_inquiries import _freeform_color_wishes
        for text, name in (('Можна колір мокко?', 'мокко'), ('Футболка кольору гірчичний', 'гірчичний'),
                           ('Хочу гірчичний колір худі', 'гірчичний'), ('Хочу худі кольору м’ятний', 'м’ятний'),
                           ('Do you have a hoodie in color mustard?', 'mustard')):
            with self.subTest(text=text):
                self.assertEqual(_freeform_color_wishes(text), [name])

    def test_common_color_declensions_remain_exact_pending_source_choices(self):
        from types import SimpleNamespace
        from management.services.ig_response_plan import _pending_stock_answer_covered, _acknowledges_choice
        forms = {'black': ('чорному', 'чорній', 'чёрным', 'черном'),
            'white': ('білому', 'білою', 'белым'), 'blue': ('синьому', 'синьою', 'синем', 'синей'),
            'pink': ('рожевому', 'рожевою', 'розовым'), 'grey': ('сірому', 'сірою', 'серым'),
            'green': ('зеленому', 'зеленою', 'зелёным'), 'coyote': ('койотовому', 'койотовою', 'койотовым')}
        for color, aliases in forms.items():
            plan = SimpleNamespace(pending_stock_questions={'source_message_ids': [7],
                'line_id': 'selected', 'choices': {'color': color, 'size': 'M'},
                'title': 'Restock Aurora', 'readiness': {'product': {'id': 12, 'category_slug': 'hoodies'}}})
            for alias in aliases:
                text = f'Зараз уточню у менеджера наявність худі Restock Aurora у {alias} кольорі та розмірі M!'
                with self.subTest(color=color, alias=alias):
                    self.assertTrue(_pending_stock_answer_covered(plan, text, 7))
                    self.assertTrue(_acknowledges_choice('color', color, f'Ваше побажання — {alias} колір.', {}))
                    for unsafe in (f'«{text}»', text.replace('уточню', 'не уточню'),
                                   text.replace(alias, 'невідомому'), text.replace('Restock Aurora', 'Storm')):
                        self.assertFalse(_pending_stock_answer_covered(plan, unsafe, 7), unsafe)
                    self.assertFalse(_pending_stock_answer_covered(plan, text, 8))
                    self.assertFalse(_acknowledges_choice('color', color, f'Ваше побажання — {alias}ватий колір.', {}))


@override_settings(IG_FULFILLMENT_REVIEWS_ENABLED=True, GOOGLE_INDEXING_ENABLED=False)
class NativeStockRequestScopeTests(waiting_fixtures.RestockWaitingListFixture, TransactionTestCase):
    def add_second(self, stock=5):
        variant = self.variants[1]
        variant.stock = stock if stock is not None else 0
        variant.save(update_fields=['stock'])
        ProductInventoryPolicy.objects.create(product=self.products[1], source='catalog_variant')
        VariantSizeRule.objects.create(variant=variant, fit_code='classic', size='L', stock=stock, is_enabled=True)
        self.canonical('добавьте Storm, количество 2, размер L, цвет pink, крій classic')

    def invitation(self):
        result, source, revision, token = self.request('Повідомте, коли всі позиції будуть в наявності')
        self.assertTrue(result.invitation_id, result)
        invitation = restock.Invitation.objects.get(pk=result.invitation_id)
        return result, source, revision, token, invitation

    def accept(self, result, source, revision, token, invitation):
        self.sent_card(revision, token, source, result)
        restock.record_delivery(revision.pk, now=self.now)
        answer, response_source, _, _ = self.reply(invitation)
        self.assertEqual(answer.reason, 'accept', answer)
        return {'invitation': invitation,
                'subscription': restock.Subscription.objects.get(accepted_answer__source=response_source)}

    def test_positive_and_zero_ledger_rows_both_remain_in_all_target(self):
        self.add_second(5)
        result, source, revision, _, invitation = self.invitation()
        self.assertEqual({row['catalog_target']['product_id'] for row in invitation.target['items']}, {row.pk for row in self.products})
        self.assertEqual(sorted(row['quantity'] for row in invitation.target['items']), [1, 2])
        self.assertEqual(len(result.quick_replies), 1)
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertFalse(restock.AvailabilityEvent.objects.exists())
        again = restock.prepare_request(revision, source, settings_obj=self.settings, now=self.now)
        self.assertEqual(again.invitation_id, result.invitation_id)
        self.assertEqual(restock.Invitation.objects.count(), 1)

    def test_all_positive_ledger_is_not_manager_confirmation(self):
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=['stock'])
        self.rule.save(update_fields=['stock'])
        self.add_second(5)
        _, _, _, _, invitation = self.invitation()
        self.assertEqual(len(invitation.target['items']), 2)
        self.assertFalse(restock.Subscription.objects.exists())
        self.assertFalse(restock.AvailabilityEvent.objects.exists())

    def test_single_positive_ledger_interest_uses_conditional_manager_checked_notification(self):
        self.variant.stock = self.rule.stock = 5
        self.variant.save(update_fields=['stock'])
        self.rule.save(update_fields=['stock'])
        result, source, revision, token = self.request()
        self.assertTrue(result.invitation_id, result)
        invitation = restock.Invitation.objects.get(pk=result.invitation_id)
        self.assertFalse(restock.Subscription.objects.exists())
        self.sent_card(revision, token, source, result)
        restock.record_delivery(revision.pk, now=self.now)
        answer, _, _, _ = self.reply(invitation)
        self.assertEqual(answer.reason, 'accept', answer)
        self.assertFalse(restock.AvailabilityEvent.objects.exists())

    def test_unknown_second_row_never_silently_subscribes_only_first(self):
        self.add_second(None)
        result, _, _, _ = self.request('Повідомте, коли всі позиції будуть в наявності')
        self.assertEqual(result.reason, 'manager_review_required')
        self.assertFalse(result.handled)
        self.assertFalse(restock.Invitation.objects.exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_exact_zero_ledger_size_keeps_only_physical_availability_debt(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        from management.services.ig_fulfillment_reviews import prepare_fulfillment_review
        from management.ig_fulfillment_review_models import IgFulfillmentReviewRequest
        source = self.source('Чи є це худі в розмірі M у наявності?')
        revision, _ = self.revision(source)
        plan = capture_response_plan(self.client_row, revision=revision)
        disposition = product_inquiries_for_revision(revision, response_plan=plan)
        self.assertFalse([row for row in disposition['inquiries'] if row['axis'] in {'size', 'color'}])
        physical = prepare_fulfillment_review(revision, settings_obj=self.settings, now=self.now)
        self.assertTrue(physical['ready'], physical)
        request = IgFulfillmentReviewRequest.objects.get(pk=physical['request_id'])
        row, = request.envelope['lines']
        self.assertEqual((row['target']['size'], row['target']['color_variant_id']), ('M', self.variant.pk))
        self.assertFalse(row['observation']['status'] == 'available')
        self.assertFalse(IgFollowUpTask.objects.filter(reason='revision_case:product_configuration_review').exists())
        self.assertFalse(restock.Subscription.objects.exists())

    def test_positive_ledger_item_still_needs_its_physical_all_check(self):
        self.add_second(5)
        case = self.accept(*self.invitation())
        _, decision = self.physically_check(case, unavailable_index=1)
        self.assertTrue(decision['ready'], decision)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))['created'], 0)
        self.assertFalse(restock.AvailabilityEvent.objects.exists())
        self.assertEqual(len(case['invitation'].target['items']), 2)
        self.variant.refresh_from_db()
        self.variants[1].refresh_from_db()
        self.assertEqual((self.variant.stock, self.variants[1].stock), (0, 5))

    def test_mixed_accounting_cart_sends_once_only_after_all_physical_checks(self):
        from management.models import IgCheckoutInventoryReservation
        from orders.models import Order
        self.add_second(5)
        case = self.accept(*self.invitation())
        _, decision = self.physically_check(case)
        self.assertTrue(decision['ready'], decision)
        self.assertEqual(restock.reconcile_subscriptions(now=self.now + timedelta(seconds=2))['created'], 1)
        count, physical = self.dispatch()
        self.assertEqual((count, len(physical)), (1, 1))
        self.assertEqual(self.dispatch(now=self.now + timedelta(seconds=4)), (0, []))
        self.assertFalse(Order.objects.exists())
        self.assertFalse(IgCheckoutInventoryReservation.objects.exists())
        self.variant.refresh_from_db()
        self.variants[1].refresh_from_db()
        self.assertEqual((self.variant.stock, self.variants[1].stock), (0, 5))


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, GOOGLE_INDEXING_ENABLED=False)
class NativeUnlistedProductRequestTests(TransactionTestCase):
    reset_sequences = True
    setUp = live.RevisionLiveTests.setUp
    _message = live.RevisionLiveTests._message
    _prepare = live.RevisionLiveTests._prepare
    _execute = live.RevisionLiveTests._execute
    _replace_bundle = live.RevisionLiveTests._replace_bundle
    _generate = contract.NativeLiveConversationContractTests._generate
    _fresh = contract.NativeLiveConversationContractTests._fresh
    _answer = contract.NativeLiveConversationContractTests._answer
    _select_product = contract.NativePendingProductInformationBoundaryTests._select_product

    def test_ordinary_catalog_palette_question_creates_no_false_color_manager_case(self):
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        self._select_product(1)
        for text in ('Які кольори худі є?', 'Покажіть кольори худі', 'What colors do you have for this hoodie?'):
            with self.subTest(text=text):
                self._fresh(text)
                plan = capture_response_plan(self.customer, revision=self.revision)
                disposition = product_inquiries_for_revision(self.revision, response_plan=plan)
                self.assertFalse(any(row['axis'] == 'color' for row in disposition['inquiries']))
                self.assertFalse(IgFollowUpTask.objects.filter(reason='revision_case:product_configuration_review').exists())

    def test_ordinary_palette_question_sends_without_a_configuration_manager_case(self):
        self._select_product(1)
        for text, reply in (('Які кольори худі є?', 'Палітру кольорів можна переглянути на сторінці моделі.'),
                            ('What colors do you have for this hoodie?', 'You can view the palette on the product page.')):
            with self.subTest(text=text):
                self._fresh(text)
                self._answer(reply)
                self.assertFalse(IgFollowUpTask.objects.filter(reason='revision_case:product_configuration_review').exists())

    def test_explicit_current_sku_composition_sends_without_a_manager_case(self):
        product = self._select_product(1)
        product.category.name, product.category.slug = 'Худі', 'hoodies'
        product.category.save(update_fields=['name', 'slug'])
        self._fresh(f'Який склад худі {product.title}?')
        self._answer(f'Склад худі {product.title}: 90% бавовни / 10% поліестеру.')
        self.assertFalse(IgFollowUpTask.objects.filter(client=self.customer).exists())

    def color_case(self, text, wanted):
        product = self._select_product(1)
        self._fresh(text)
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        plan = capture_response_plan(self.customer, revision=self.revision)
        captured = product_inquiries_for_revision(self.revision, response_plan=plan)
        self.assertEqual(captured['status'], 'manager_required', {
            'readiness': plan.readiness_snapshot, 'active_line': plan.source_cart_capture.get('active_line_id'),
            'lines': [{'id': row.line_id, 'colors': row.as_dict()['authority'].get('colors'),
                       'configuration': row.as_dict()['configuration']} for row in plan.line_plans]})
        self._answer(f'Ваше побажання — худі кольору {wanted}. Уточню у менеджера можливість його виконати.', manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:product_configuration_review')
        row, = task.manager_context['product_inquiries']['inquiries']
        self.assertEqual(row['axis'], 'color')
        self.assertEqual(row['requested_value'], wanted)
        self.assertEqual(row['product_id'], product.pk)
        self.assertEqual(row['source_message_id'], self.source.pk)
        self.assertIn(text.rstrip('.'), row['question'])
        self.assertFalse(row['catalog_authority'])
        self.assertFalse(row['checkout_authority'])
        result, model, http = self._execute()
        self.assertEqual(result.state, 'completed', result.reasons)
        model.assert_not_called()
        http.assert_not_called()
        self.assertEqual(IgFollowUpTask.objects.filter(reason='revision_case:product_configuration_review').count(), 1)

    def test_recognized_missing_purple_uses_existing_manager_case(self):
        self.color_case('Хочу худі кольору purple.', 'purple')

    def test_freeform_mocha_uses_existing_manager_case(self):
        self.color_case('Хочу худі кольору мокко.', 'мокко')

    def test_freeform_mustard_uses_existing_manager_case(self):
        self.color_case('Хочу худі кольору гірчичний.', 'гірчичний')

    def test_freeform_mint_apostrophe_is_preserved_for_manager(self):
        self.color_case('Хочу худі кольору м’ятний.', 'м’ятний')

    def test_affirmative_color_correction_after_unrelated_negation_keeps_only_latest_wish(self):
        from django.utils import timezone
        from management.models import IgCustomerTurn, IgTurnMessage
        from management.services.ig_turn_revisions import create_collecting_revision
        from management.services.ig_response_plan import capture_response_plan
        from management.services.ig_product_inquiries import product_inquiries_for_revision
        product = self._select_product(1)
        old = self._message('Хочу худі кольору мокко', 'color-old-mocha')
        latest = self._message('Не хочу regular, хочу худі кольору гірчичний', 'color-new-mustard')
        self.turn = IgCustomerTurn.objects.create(client=self.customer, primary_source_message=old,
            window_started_at=timezone.now(), window_deadline=timezone.now())
        for index, source in enumerate((old, latest), 1):
            IgTurnMessage.objects.create(turn=self.turn, message=source, ordinal=index, role='user')
        self.revision = create_collecting_revision(self.turn, [old, latest], bypass_quiet=True).revision
        self.source = latest
        self._prepare()
        plan = capture_response_plan(self.customer, revision=self.revision)
        disposition = product_inquiries_for_revision(self.revision, response_plan=plan)
        inquiry, = disposition['inquiries']
        self.assertEqual((inquiry['requested_value'], inquiry['source_message_id'], inquiry['product_id']),
                         ('гірчичний', latest.pk, product.pk))
        witness, = disposition['superseded_color_wishes']
        self.assertEqual((witness['previous_source_message_id'], witness['current_source_message_id'],
                          witness['previous_value'], witness['current_value'], witness['product_id'], witness['line_id']),
                         (old.pk, latest.pk, 'мокко', 'гірчичний', product.pk, inquiry['line_id']))
        self.assertTrue(witness['previous_source_sole_color_wish'])
        self._answer('Зрозуміло, regular не підходить. Ваше побажання — худі кольору гірчичний. '
                     'Уточню у менеджера можливість його виконати. Який крій ви бажаєте?', manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:product_configuration_review')
        row, = task.manager_context['product_inquiries']['inquiries']
        self.assertEqual((row['requested_value'], row['source_message_id']), ('гірчичний', latest.pk))

    def test_unknown_lining_question_reaches_exact_manager_information_case(self):
        self._select_product(1)
        self._fresh('Яка підкладка у цього худі?')
        self._answer('Уточню підкладку у менеджера.', manager=True)
        task = IgFollowUpTask.objects.get(reason='revision_case:product_information_review')
        row, = task.manager_context['product_inquiries']['inquiries']
        self.assertEqual(row['axis'], 'information')
        self.assertIn('підкладка', row['question'])
        self.assertEqual(row['source_message_id'], self.source.pk)


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE='unified',
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeZeroLedgerDeliveryTests(waiting_fixtures.RestockWaitingListFixture, TransactionTestCase):
    reset_sequences = True
    _generate = live.RevisionLiveTests._generate
    _execute = live.RevisionLiveTests._execute

    def test_known_zero_ledger_size_sends_one_pending_reply_and_one_exact_review(self):
        from unittest.mock import patch
        from management.models import IgBotNotification
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = 'observed'
        self.provider_failure = False
        source = self.source('Чи є це худі в розмірі M у наявності?')
        revision, token = self.revision(source)
        self.revision, self.token = revision, token
        self.parsed = {'reply_text': 'Уточню у менеджера наявність худі Restock Aurora у розмірі M.',
                       'controls': [{'kind': 'manager', 'value': True}]}
        with patch('management.services.ig_presence.start_presence', return_value=None), patch(
                'socket.socket.connect', side_effect=AssertionError('stock test network forbidden')):
            result, model, http = self._execute()
        self.revision.refresh_from_db()
        coverage = self.revision.action_receipts.get('response_coverage') or {}
        self.assertEqual(result.state, 'completed', (result.reasons, coverage))
        self.assertEqual((model.call_count, http.call_count), (1, 1))
        self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
        self.assertFalse(coverage.get('remaining'), coverage)
        task, = IgFollowUpTask.objects.filter(client=self.customer)
        self.assertEqual(task.reason, 'fulfillment_review')
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (0, 0))


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE='unified',
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeApprovedBaseWishBindingTests(waiting_fixtures.RestockWaitingListFixture, TransactionTestCase):
    """An approved answer and exact pending wish share their captured SKU."""
    reset_sequences = True
    _generate = live.RevisionLiveTests._generate
    _execute = live.RevisionLiveTests._execute
    reply = ('Склад цієї основи: 90% бавовни / 10% поліестеру. Щодо кольору мокко — '
             'я передам ваше побажання менеджеру, щоб він уточнив можливість.')

    def setUp(self):
        from unittest.mock import patch
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        original = SourceCartCatalogTests.setUp
        def reviewed_catalog(case):
            original(case)
            case.category.name, case.category.slug = 'Худі', 'hoodies'
            case.category.save(update_fields=['name', 'slug'])
        with patch.object(SourceCartCatalogTests, 'setUp', reviewed_catalog):
            super().setUp()
        for target, kwargs in (('socket.socket.connect', {'side_effect': AssertionError('binding test network forbidden')}),
                ('management.services.ig_presence.start_presence', {'return_value': None})):
            guard = patch(target, **kwargs)
            guard.start()
            self.addCleanup(guard.stop)
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count = 0
        self.actual_hash_override = 'observed'
        self.provider_failure = False
        from management.services.ig_revision_live import RevisionGenerationBoundary
        original_init = RevisionGenerationBoundary.__init__
        def capture_boundary(boundary, *args, **kwargs):
            original_init(boundary, *args, **kwargs)
            self.boundary = boundary
        boundary_patch = patch.object(RevisionGenerationBoundary, '__init__', capture_boundary)
        boundary_patch.start()
        self.addCleanup(boundary_patch.stop)
        self.variant.stock = self.rule.stock = 100
        self.variant.save(update_fields=['stock'])
        self.rule.save(update_fields=['stock'])
        self.choice = self.canonical('Хочу худі Restock Aurora, кількість 1, розмір M, колір black, крій classic')
        self.source_row = self.source('Хочу це худі кольору мокко.')
        self.revision, self.token = waiting_fixtures.RestockWaitingListFixture.revision(self, self.source_row)
        self.parsed = {'reply_text': self.reply, 'controls': [{'kind': 'manager', 'value': True}],
            'turn_intelligence': {'catalog_candidates': [{'evidence': 'Restock Aurora hoodie',
                'product_id': self.products[0].pk, 'confidence': 1.0}], 'transcript': '',
                'intent': 'product_configuration_review', 'confidence': 1.0}}

    def test_current_approved_composition_has_exact_wish_anchor(self):
        from management.services.ig_response_plan import _approved_base_wish_anchors
        from management.services.ig_response_control import parse_structured_response
        self._execute()
        plan = self.boundary.response_plan
        self.assertTrue(_approved_base_wish_anchors(plan, parse_structured_response(self.parsed)), plan.as_dict())
        from management.services.ig_reply_authority import build_reply_truth_context
        self.assertEqual(plan.validate_multiline_claims(parse_structured_response(self.parsed),
            build_reply_truth_context(self.customer)), '')

    def test_quoted_negative_missing_wish_or_manager_does_not_gain_anaphoric_coverage(self):
        from management.services.ig_response_plan import _approved_base_wish_anchors
        from management.services.ig_response_control import parse_structured_response
        self._execute()
        plan = self.boundary.response_plan
        composition, handoff = self.reply.split('. ', 1)
        for text, manager in ((f'«{composition}.» {handoff}', True),
                (f'Не {composition}. {handoff}', True),
                (f'{composition}. «{handoff}»', True),
                (self.reply.replace('я передам', 'я не передам'), True),
                (self.reply.replace('можливість.', 'можливість?'), True),
                (self.reply.replace('мокко', 'гірчичний'), True),
                (f'{composition}.', True), (self.reply, False)):
            with self.subTest(text=text, manager=manager):
                response = parse_structured_response({'reply_text': text,
                    'controls': [{'kind': 'manager', 'value': True}] if manager else []})
                self.assertFalse(_approved_base_wish_anchors(plan, response))
                garment_ids = {row['id'] for row in plan.obligations if row['kind'] == 'garment_type'}
                self.assertTrue(garment_ids.intersection(plan.coverage(response)['remaining']))

    def test_missing_stale_or_foreign_proof_cannot_bind_an_approved_composition(self):
        from copy import deepcopy
        from dataclasses import replace
        from management.services.ig_product_inquiries import _digest
        from management.services.ig_response_plan import _approved_base_wish_anchors
        from management.services.ig_response_control import parse_structured_response
        self._execute()
        plan = self.boundary.response_plan
        response = parse_structured_response(self.parsed)
        mutations = {
            'missing_fact': lambda data: data.update(base_context_assertions=[]),
            'missing_wish': lambda data: data.update(inquiries=[]),
            'foreign_sku': lambda data: data['inquiries'][0].update(product_id=self.products[1].pk),
            'foreign_line': lambda data: data['inquiries'][0].update(line_id='other-line'),
            'stale_source': lambda data: data['inquiries'][0].update(source_message_id=self.choice.pk),
            'different_digest': lambda data: data['inquiries'][0].update(source_digest='f' * 64),
            'stale_fact': lambda data: data['base_context_assertions'][0].update(publication_version='obsolete'),
            'foreign_fact': lambda data: data['base_context_assertions'][0].update(product_id=self.products[1].pk),
            'different_cart': lambda data: data.update(cart_identity='e' * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                captured = deepcopy(plan.product_information)
                mutate(captured)
                captured['digest'] = _digest({key: value for key, value in captured.items()
                    if key not in {'digest', 'snapshot_digest'}})
                candidate = replace(plan, product_information=captured)
                self.assertFalse(_approved_base_wish_anchors(candidate, response))
                self.assertTrue(candidate.coverage(response)['remaining'])
        self.assertFalse(_approved_base_wish_anchors(replace(plan,
            line_plans=plan.line_plans + plan.line_plans), response))

    def test_approved_composition_anchor_does_not_answer_price_product_or_checkout(self):
        from dataclasses import replace
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_reply_authority import build_reply_truth_context
        self._execute()
        plan = self.boundary.response_plan
        extra = tuple({'id': f'extra:{kind}', 'kind': kind, 'line_id': plan.line_plans[0].line_id,
            'source_message_id': self.source_row.pk} for kind in ('product_id', 'info:price', 'purchase_requested'))
        candidate = replace(plan, obligations=plan.obligations + extra)
        coverage = candidate.coverage(parse_structured_response(self.parsed))
        self.assertTrue({row['id'] for row in extra}.issubset(coverage['remaining']), coverage)
        forbidden = parse_structured_response({**self.parsed, 'reply_text': self.reply + ' Ціна — 1 грн.'})
        self.assertEqual(plan.validate_multiline_claims(forbidden, build_reply_truth_context(self.customer)),
            'response_plan_line_claim_unverified')

    def test_native_different_color_handoff_cannot_send_naked_composition(self):
        from unittest.mock import patch
        self.parsed['reply_text'] = self.reply.replace('мокко', 'гірчичний')
        with patch('management.services.ig_presence.start_presence', return_value=None), patch(
                'socket.socket.connect', side_effect=AssertionError('binding test network forbidden')):
            result, model, http = self._execute()
        self.revision.refresh_from_db()
        self.assertEqual(result.state, 'blocked', result.reasons)
        http.assert_not_called()
        self.assertFalse(self.revision.delivery_effects.filter(state='sent').exists())
        self.assertTrue((self.revision.action_receipts.get('response_coverage') or {}).get('remaining'))

    def test_final_fact_bridge_retains_quote_cross_line_and_material_guards(self):
        import json
        from dataclasses import replace
        from management.services.ig_reply_authority import build_reply_truth_context
        from management.services.ig_response_cart_plan import ResponseLinePlan
        from management.services.ig_response_control import parse_structured_response
        from management.services.ig_revision_live import _captured_reply_truth
        self._execute()
        plan = self.boundary.response_plan
        context = build_reply_truth_context(self.customer)
        for text in ('Склад худі Restock Aurora: 100% бавовни.',
                     'Склад футболки Storm: 90% бавовни / 10% поліестеру.',
                     'Худі Restock Aurora виготовлене із шовку.'):
            with self.subTest(text=text):
                response = parse_structured_response({'reply_text': text, 'controls': []})
                self.assertTrue(plan.validate_multiline_claims(response, context))
                self.assertFalse(_captured_reply_truth(plan, response, context).valid)
                self.assertFalse(plan.answered_product_assertions(response, context))
        quoted = parse_structured_response({'reply_text':
            '«Склад худі Restock Aurora: 90% бавовни / 10% поліестеру.»', 'controls': []})
        self.assertFalse(plan.answered_product_assertions(quoted, context))
        self.assertTrue(plan.coverage(quoted)['remaining'])
        sibling = plan.line_plans[0].as_dict()
        sibling.update(line_id='sibling', index=1, recipient_id='other')
        sibling['choices'].update(product_id=self.products[1].pk, garment_type='tshirt')
        sibling['configuration'].update(product_id=self.products[1].pk, product_title='Storm')
        cross_line = replace(plan, line_plans=plan.line_plans + (ResponseLinePlan(json.dumps(sibling)),))
        response = parse_structured_response({'reply_text':
            'Склад футболки Storm: 90% бавовни / 10% поліестеру.', 'controls': []})
        self.assertTrue(cross_line.validate_multiline_claims(response, context))
        self.assertFalse(_captured_reply_truth(cross_line, response, context).valid)

    def test_actual_anaphoric_composition_and_mocha_handoff_sends_once_without_debt(self):
        from unittest.mock import patch
        from management.models import IgBotNotification
        with patch('management.services.ig_presence.start_presence', return_value=None), patch(
                'socket.socket.connect', side_effect=AssertionError('binding test network forbidden')):
            result, model, http = self._execute()
            self.revision.refresh_from_db()
            coverage = self.revision.action_receipts.get('response_coverage') or {}
            self.assertEqual(result.state, 'completed', (result.reasons, coverage))
            self.assertEqual((model.call_count, http.call_count), (1, 1))
            self.assertEqual(self.revision.delivery_effects.filter(state='sent').count(), 1)
            self.assertEqual(self.revision.delivery_effects.get(state='sent').payload['message']['text'], self.reply)
            self.assertFalse(coverage.get('remaining'), coverage)
            task, = IgFollowUpTask.objects.filter(client=self.customer)
            self.assertEqual(task.reason, 'revision_case:product_configuration_review')
            inquiry, = task.manager_context['product_inquiries']['inquiries']
            self.assertEqual((inquiry['product_id'], inquiry['requested_value'], inquiry['source_message_id']),
                (self.products[0].pk, 'мокко', self.source_row.pk))
            self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
            repeated, model2, http2 = self._execute()
        self.assertEqual(repeated.state, 'completed', repeated.reasons)
        model2.assert_not_called()
        http2.assert_not_called()
        self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer).count(), 1)
        self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
        from management.services.ig_commerce_projection import capture_current_selection_lines
        selected, = capture_current_selection_lines(self.customer.pk)['lines']
        self.assertEqual((selected['fields']['color']['value'], selected['fields']['size']['value']), ('black', 'M'))
        self.variant.refresh_from_db()
        self.rule.refresh_from_db()
        self.assertEqual((self.variant.stock, self.rule.stock), (100, 100))


@override_settings(IG_REVISION_EXECUTION_ENABLED=False, IG_FULFILLMENT_REVIEWS_ENABLED=True,
    IG_RESTOCK_SUBSCRIPTIONS_ENABLED=True, IG_TURN_CONTEXT_MODE='unified',
    GOOGLE_INDEXING_ENABLED=False, INDEXNOW_ENABLED=False)
class NativeZeroLedgerPriceContinuationTests(waiting_fixtures.RestockWaitingListFixture, TransactionTestCase):
    """Known catalogue price remains separate from pending physical stock."""
    from management.tests_ig_revision_selection_noop_20261023 import NativeSelectionNoOpTests as _NoOp
    reset_sequences = True
    _execute = _NoOp._execute
    candidate, prepare, domain_state = _NoOp.candidate, _NoOp.prepare, _NoOp.domain_state

    def _generate(self, payload, **kwargs):
        from management.models import IgCommerceSelectionSession
        self.generation_head = IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1).snapshot()
        if getattr(self, 'reject_candidate', False):
            from management.services.call_ai_analysis import CallAIAnalysisError
            if self.before_validate:
                self.before_validate()
            decision = kwargs['result_validator'](self.parsed,
                usage={'_request_inline_count': 0, '_request_inline_content_hashes': []})
            self.validation_reasons = decision.reason_codes
            self.assertFalse(decision.valid)
            raise CallAIAnalysisError('expected deterministic price/source rejection')
        return self._NoOp._generate(self, payload, **kwargs)

    def setUp(self):
        from unittest.mock import patch
        from management.tests_ig_source_cart_catalog import SourceCartCatalogTests
        original = SourceCartCatalogTests.setUp
        def catalog(case):
            original(case)
            case.category.name, case.category.slug = 'Худі', 'hoodies'
            case.category.save(update_fields=['name', 'slug'])
            case.products[0].price = case.variants[0].price_override = 900
            case.products[0].save(update_fields=['price'])
            case.variants[0].save(update_fields=['price_override'])
        with patch.object(SourceCartCatalogTests, 'setUp', catalog):
            super().setUp()
        guard = patch('socket.socket.connect', side_effect=AssertionError('price test network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.choice = self.canonical('Хочу худі Restock Aurora, кількість 1, розмір M, колір black, крій classic')
        self.settings.ai_enabled = True
        self.settings.save(update_fields=['ai_enabled'])
        self.customer = self.client_row
        self.generation_calls = []
        self.before_validate = self.after_validate = None
        self.actual_inline_count, self.actual_hash_override = 0, 'observed'
        self.provider_failure = False
        self.action_calls = []

    def price_continuation(self, controls):
        from management.models import IgBotNotification
        from management.services.ig_commerce_projection import capture_current_selection_lines
        result, _, sent = self.prepare()
        self.assertEqual(result.state, 'completed', result.reasons)
        self.assertEqual(sent.call_count, 1)
        original = capture_current_selection_lines(self.customer.pk)
        for stock in (0, 100):
            with self.subTest(stock=stock, controls=controls):
                self.variant.stock = self.rule.stock = stock
                self.variant.save(update_fields=['stock'])
                self.rule.save(update_fields=['stock'])
                parsed = {'reply_text': 'Ціна Restock Aurora — 900 грн.',
                    'controls': self.candidate()['controls'][:-1] if controls else []}
                result, model, http = self.prepare(question='А ціна?', parsed=parsed)
                self.assertEqual(result.state, 'completed', (result.reasons, self.validation_reasons))
                self.assertEqual((model.call_count, http.call_count), (1, 1))
                self.assertEqual(self.revision.delivery_effects.get().payload['message']['text'], parsed['reply_text'])
                self.assertFalse(self.revision.action_receipts['response_coverage']['remaining'])
                current = capture_current_selection_lines(self.customer.pk)
                self.assertEqual([row['fields'] for row in current['lines']],
                    [row['fields'] for row in original['lines']])
                from management.models import IgCommerceSelectionSession
                self.assertEqual(IgCommerceSelectionSession.objects.get(client=self.customer, open_slot=1).snapshot(),
                    self.generation_head)
                if controls:
                    self.assertEqual(self.revision.action_receipts['client_configuration_update']['outcome'], 'no_op')
                self.assertEqual(IgFollowUpTask.objects.filter(client=self.customer, reason='fulfillment_review').count(), 1)
                self.assertEqual(IgBotNotification.objects.filter(client=self.customer).count(), 1)
                self.assertEqual(self.domain_state()['purchase_counts'], (0, 0, 0, 0))
                self.assertEqual(self.domain_state()['stock'], (stock, stock))
                repeated, generated, transport = self._execute()
                self.assertEqual(repeated.state, 'completed', repeated.reasons)
                generated.assert_not_called()
                transport.assert_not_called()

    def test_zero_pending_stock_then_known_price_without_controls(self):
        self.price_continuation(False)

    def test_zero_pending_stock_then_known_price_with_redundant_current_controls(self):
        self.price_continuation(True)

    def test_zero_ledger_price_projection_preserves_readiness_and_disabled_size_gate(self):
        from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
        from management.services.ig_commerce_projection import capture_current_selection_lines
        def catalog():
            return resolve_source_cart_catalog(capture_current_selection_lines(self.customer.pk))
        captured = catalog()
        context, = captured.contexts_by_line.values()
        self.assertEqual(tuple(map(str, context.authorized_prices)), ('900.00',))
        self.assertFalse(context.allowed_sizes or context.allowed_fits or context.allowed_colors)
        self.assertFalse(captured.item_specs_by_line)
        readiness, = captured.readiness_by_line.values()
        self.assertFalse(readiness['can_issue_link'])
        self.assertFalse(readiness['applicability_known'])
        from copy import deepcopy
        original = capture_current_selection_lines(self.customer.pk)
        unknown_size = deepcopy(original)
        unknown_size['lines'][0]['fields']['size']['value'] = 'XXL'
        self.assertTrue(all(not row.authorized_prices for row in
            resolve_source_cart_catalog(unknown_size).contexts_by_line.values()))
        self.assertFalse(resolve_source_cart_catalog({}).contexts_by_line)
        self.rule.is_enabled = False
        self.rule.save(update_fields=['is_enabled'])
        self.assertFalse(next(iter(catalog().contexts_by_line.values())).authorized_prices)
        self.rule.is_enabled = True
        self.rule.save(update_fields=['is_enabled'])
        self.product.status = 'draft'
        self.product.save(update_fields=['status'])
        missing = catalog()
        self.assertTrue(missing.gaps)
        self.assertTrue(all(not row.authorized_prices for row in missing.contexts_by_line.values()))

    def test_known_price_cannot_borrow_another_source_lines_price(self):
        from management.services.ig_source_cart_catalog import resolve_source_cart_catalog
        from management.services.ig_commerce_projection import capture_current_selection_lines
        self.products[1].price = self.variants[1].price_override = 990
        self.products[1].save(update_fields=['price'])
        self.variants[1].save(update_fields=['price_override'])
        NativeStockRequestScopeTests.add_second(self, stock=0)
        captured = resolve_source_cart_catalog(capture_current_selection_lines(self.customer.pk))
        self.assertEqual([tuple(map(str, row.authorized_prices)) for row in captured.contexts_by_line.values()],
            [('900.00',), ('990.00',)])
        self.assertFalse(captured.item_specs_by_line)

    def test_wrong_price_foreign_product_and_stale_source_send_nothing(self):
        from management.models import InstagramBotMessage
        first, _, sent = self.prepare()
        self.assertEqual(first.state, 'completed', first.reasons)
        self.assertEqual(sent.call_count, 1)
        self.reject_candidate = True
        for text in ('Ціна Restock Aurora — 901 грн.', 'Ціна Storm — 900 грн.'):
            with self.subTest(text=text):
                result, _, http = self.prepare(question='А ціна?', parsed={'reply_text': text, 'controls': []})
                self.assertNotEqual(result.state, 'completed', result.reasons)
                http.assert_not_called()
                self.assertIn('response_plan_line_claim_unverified', self.validation_reasons)
        self.before_validate = lambda: InstagramBotMessage.objects.filter(pk=self.source_row.pk).update(
            text='Відредаговане поточне запитання')
        result, _, http = self.prepare(question='А ціна?', parsed={
            'reply_text': 'Ціна Restock Aurora — 900 грн.', 'controls': []})
        self.assertNotEqual(result.state, 'completed', result.reasons)
        http.assert_not_called()
        self.assertTrue(self.validation_reasons)
        self.assertEqual(self.domain_state()['purchase_counts'], (0, 0, 0, 0))
