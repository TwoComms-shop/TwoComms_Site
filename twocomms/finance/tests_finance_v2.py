from decimal import Decimal
import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    Account, Category, Company, Counterparty, CounterpartyCard, FundingSource,
    ObligationComponent, ObligationGroup, ObligationSettlement, Transaction, InternalTransferMatch,
)
from .services import ledger_v2, obligations_v2, payment_intents


@override_settings(ALLOWED_HOSTS=['testserver', 'fin.twocomms.shop'], SECURE_SSL_REDIRECT=False)
class FinanceV2ServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('finance-v2', 'v2@example.test', 'x')
        self.company = Company.objects.create(name='Finance V2', base_currency='UAH')
        self.account = Account.objects.create(company=self.company, name='ФОП', currency='UAH')
        self.cash = Account.objects.create(company=self.company, name='Наличные', type='cash', currency='UAH')
        self.category = Category.objects.create(company=self.company, name='Оборудование', type='expense')
        self.cp = Counterparty.objects.create(company=self.company, name='Налоговая', type='government')
        self.card_a = CounterpartyCard.objects.create(company=self.company, counterparty=self.cp,
                                                     iban='UA111', label='Военный сбор')
        self.card_b = CounterpartyCard.objects.create(company=self.company, counterparty=self.cp,
                                                     iban='UA222', label='Единый налог')

    def _txn(self, ttype, amount, *, account=None, comment=''):
        return Transaction.objects.create(
            company=self.company, type=ttype, status=Transaction.STATUS_ACTUAL,
            amount=amount, amount_base=amount, currency='UAH', account=account or self.account,
            date_actual=timezone.now(), comment=comment,
        )

    def test_grant_allocation_and_classification_are_reviewable(self):
        grant = self._txn(Transaction.TYPE_INCOME, Decimal('216000'), comment='Грант')
        source = FundingSource.objects.create(company=self.company, name='Грант 2026',
                                              received_amount=Decimal('216000'))
        ledger_v2.classify_transaction(grant, user=self.user, ownership_scope='business',
                                       economic_kind='grant_inflow', funding_source=source)
        equipment = self._txn(Transaction.TYPE_EXPENSE, Decimal('10000'))
        equipment.category = self.category
        equipment.save(update_fields=['category'])
        ledger_v2.allocate_funding(source=source, txn=equipment, amount=Decimal('7000'), user=self.user)
        source.refresh_from_db()
        grant.refresh_from_db()
        self.assertEqual(grant.economic_kind, 'grant_inflow')
        self.assertEqual(source.allocations.count(), 1)
        self.assertEqual(ledger_v2.funding_summary(source)['available'], Decimal('209000'))
        self.assertEqual(grant.ledger_classification.events.count(), 1)

    def test_grant_account_history_confirms_actual_rows_only(self):
        grant_account = Account.objects.create(company=self.company, name='Грантова', currency='UAH', is_business=True)
        source = FundingSource.objects.create(company=self.company, name='УВФ', received_amount=Decimal('100000'))
        receipt = self._txn(Transaction.TYPE_INCOME, Decimal('50000'), account=grant_account)
        expense = self._txn(Transaction.TYPE_EXPENSE, Decimal('12000'), account=grant_account)
        planned = Transaction.objects.create(
            company=self.company, type=Transaction.TYPE_EXPENSE, status=Transaction.STATUS_PLANNED,
            amount=Decimal('1000'), amount_base=Decimal('1000'), currency='UAH', account=grant_account,
            date_actual=timezone.now(),
        )
        changed = ledger_v2.confirm_grant_account_history(grant_account, source, user=self.user)
        self.assertEqual({txn.id for txn in changed}, {receipt.id, expense.id})
        receipt.refresh_from_db(); expense.refresh_from_db(); planned.refresh_from_db()
        self.assertEqual(receipt.economic_kind, 'grant_inflow')
        self.assertEqual(expense.economic_kind, 'operating_expense')
        self.assertEqual(expense.funding_source_id, source.id)
        self.assertEqual(source.allocations.filter(transaction=expense, allocation_type='spent').count(), 1)
        self.assertEqual(planned.economic_kind, 'unknown')
        ledger_v2.confirm_grant_account_history(grant_account, source, user=self.user)
        self.assertEqual(source.allocations.filter(transaction=expense, allocation_type='spent').count(), 1)

    def test_grant_classification_requires_program(self):
        grant = self._txn(Transaction.TYPE_INCOME, Decimal('216000'), comment='За реквізитами')
        self.client.force_login(self.user)
        response = self.client.post(
            f'/api/v2/transactions/{grant.id}/classification/',
            data=json.dumps({'economic_kind': 'grant_inflow', 'ownership_scope': 'business'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('грантову програму', response.json()['error'])

    def test_fop_sale_and_pension_quick_actions_are_account_aware(self):
        fop = Account.objects.create(company=self.company, name='monobank ФОП', currency='UAH', is_business=True)
        pension = Account.objects.create(company=self.company, name='Пенсійна', currency='UAH', is_business=False)
        sale = self._txn(Transaction.TYPE_INCOME, Decimal('1650'), account=fop)
        pension_income = self._txn(Transaction.TYPE_INCOME, Decimal('5000'), account=pension)
        self.client.force_login(self.user)
        response = self.client.post(
            f'/api/v2/transactions/{sale.id}/classification/',
            data=json.dumps({'quick_action': 'sale'}), content_type='application/json', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(response.status_code, 200)
        sale.refresh_from_db()
        self.assertEqual(sale.economic_kind, 'sale')
        self.assertTrue(sale.ownership_scope == 'business')
        response = self.client.post(
            f'/api/v2/transactions/{pension_income.id}/classification/',
            data=json.dumps({'quick_action': 'pension'}), content_type='application/json', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(response.status_code, 200)
        pension_income.refresh_from_db()
        self.assertEqual(pension_income.economic_kind, 'pension_income')
        self.assertEqual(pension_income.ownership_scope, 'personal')

    def test_unequal_transfer_records_fee_and_is_idempotent(self):
        outgoing = self._txn(Transaction.TYPE_EXPENSE, Decimal('10050'), account=self.cash)
        incoming = self._txn(Transaction.TYPE_INCOME, Decimal('10000'), account=self.account)
        match = ledger_v2.create_transfer_suggestion(self.company, outgoing, incoming)
        self.assertEqual(match.fee_amount, Decimal('50'))
        ledger_v2.confirm_transfer(match, user=self.user)
        match.refresh_from_db()
        self.assertEqual(match.fee_amount, Decimal('50'))
        self.assertEqual(match.status, 'confirmed')
        ledger_v2.confirm_transfer(match, user=self.user)
        self.assertEqual(InternalTransferMatch.objects.filter(id=match.id).count(), 1)

    def test_transfer_match_api_previews_and_confirms_fee(self):
        outgoing = self._txn(Transaction.TYPE_EXPENSE, Decimal('10050'), account=self.cash)
        incoming = self._txn(Transaction.TYPE_INCOME, Decimal('10000'), account=self.account)
        self.client.force_login(self.user)
        preview = self.client.get(
            f'/api/v2/transfers/match/?transaction_id={incoming.id}', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()['candidates'][0]['fee_amount'], '50.00')
        confirmed = self.client.post(
            '/api/v2/transfers/match/', data=json.dumps({
                'source_transaction_id': outgoing.id,
                'destination_transaction_id': incoming.id,
                'confirm': True,
            }), content_type='application/json', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()['match']['fee_amount'], '50.00')
        reverse_preview = self.client.get(
            f'/api/v2/transfers/match/?transaction_id={outgoing.id}', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(reverse_preview.status_code, 200)
        self.assertEqual(reverse_preview.json()['existing']['partner_transaction_id'], incoming.id)
        self.assertEqual(reverse_preview.json()['existing']['fee_amount'], '50.00')

    def test_transfer_confirmation_does_not_change_pnl_classification(self):
        outgoing = self._txn(Transaction.TYPE_EXPENSE, Decimal('10000'), account=self.cash)
        incoming = self._txn(Transaction.TYPE_INCOME, Decimal('10000'), account=self.account)
        match = ledger_v2.create_transfer_suggestion(self.company, outgoing, incoming)
        ledger_v2.confirm_transfer(match, user=self.user)
        outgoing.refresh_from_db()
        incoming.refresh_from_db()
        self.assertEqual(match.status, 'confirmed')
        self.assertEqual(outgoing.economic_kind, 'internal_transfer')
        self.assertEqual(incoming.economic_kind, 'internal_transfer')

    def test_terminal_cash_review_is_non_destructive_until_confirmed(self):
        self.cash.initial_balance = Decimal('2000')
        self.cash.current_balance = Decimal('2000')
        self.cash.save(update_fields=['initial_balance', 'current_balance'])
        top_up = self._txn(
            Transaction.TYPE_INCOME, Decimal('1250'), account=self.account,
            comment='Поповнення через термінал Mono',
        )
        top_up.external_data = {'counter_name': 'City24'}
        top_up.save(update_fields=['external_data'])
        self.client.force_login(self.user)

        candidates = self.client.get('/api/v2/terminal-cash/candidates/', HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(candidates.status_code, 200)
        payload = candidates.json()
        self.assertEqual(payload['count'], 1)
        self.assertEqual(payload['candidates'][0]['evidence']['providers'], ['monobank', 'city24'])
        self.assertEqual(payload['cash_accounts'][0]['id'], self.cash.id)

        proposed = self.client.post(
            f'/api/v2/terminal-cash/candidates/{top_up.id}/review/',
            data=json.dumps({'action': 'cash_transfer', 'source_cash_account_id': self.cash.id}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(proposed.status_code, 201)
        review_id = proposed.json()['review']['id']
        repeated_proposal = self.client.post(
            f'/api/v2/terminal-cash/candidates/{top_up.id}/review/',
            data=json.dumps({'action': 'cash_transfer', 'source_cash_account_id': self.cash.id}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(repeated_proposal.status_code, 201)
        self.assertEqual(repeated_proposal.json()['review']['id'], review_id)
        top_up.refresh_from_db()
        self.assertEqual(top_up.type, Transaction.TYPE_INCOME)
        self.assertEqual(top_up.account_id, self.account.id)
        self.assertEqual(top_up.economic_kind, 'unknown')

        accepted = self.client.post(
            f'/api/v2/reviews/{review_id}/action/', data=json.dumps({'action': 'accept'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(accepted.status_code, 200)
        top_up.refresh_from_db()
        self.assertEqual(top_up.type, Transaction.TYPE_TRANSFER)
        self.assertEqual(top_up.account_id, self.cash.id)
        self.assertEqual(top_up.to_account_id, self.account.id)
        self.assertEqual(top_up.economic_kind, 'owner_draw')
        self.assertEqual(top_up.ownership_scope, 'personal')
        self.assertEqual(top_up.external_data['terminal_cash_transfer']['source_cash_account_id'], self.cash.id)
        self.cash.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.cash.current_balance, Decimal('750'))
        self.assertEqual(self.account.current_balance, Decimal('1250'))

        repeated = self.client.post(
            f'/api/v2/reviews/{review_id}/action/', data=json.dumps({'action': 'accept'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json()['status'], 'accepted')

    def test_terminal_cash_income_review_requires_an_explicit_kind(self):
        top_up = self._txn(
            Transaction.TYPE_INCOME, Decimal('800'), account=self.account,
            comment='City24 поповнення картки',
        )
        self.client.force_login(self.user)
        invalid = self.client.post(
            f'/api/v2/terminal-cash/candidates/{top_up.id}/review/',
            data=json.dumps({'action': 'income'}), content_type='application/json',
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(invalid.status_code, 400)
        proposed = self.client.post(
            f'/api/v2/terminal-cash/candidates/{top_up.id}/review/',
            data=json.dumps({'action': 'income', 'economic_kind': 'sale', 'ownership_scope': 'business'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(proposed.status_code, 201)
        review_id = proposed.json()['review']['id']
        top_up.refresh_from_db()
        self.assertEqual(top_up.economic_kind, 'unknown')
        accepted = self.client.post(
            f'/api/v2/reviews/{review_id}/action/', data=json.dumps({'action': 'accept'}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(accepted.status_code, 200)
        top_up.refresh_from_db()
        self.assertEqual(top_up.type, Transaction.TYPE_INCOME)
        self.assertEqual(top_up.economic_kind, 'sale')

    def test_terminal_review_exposes_only_cash_as_source(self):
        own_card = Account.objects.create(
            company=self.company, name='Власна картка', type='card', currency='UAH')
        top_up = self._txn(
            Transaction.TYPE_INCOME, Decimal('900'), account=self.account,
            comment='Термінал mono',
        )
        self.client.force_login(self.user)
        payload = self.client.get(
            '/api/v2/terminal-cash/candidates/', HTTP_HOST='fin.twocomms.shop').json()
        self.assertEqual(payload['cash_accounts'][0]['id'], self.cash.id)
        self.assertNotIn(own_card.id, [account['id'] for account in payload['cash_accounts']])
        proposed = self.client.post(
            f'/api/v2/terminal-cash/candidates/{top_up.id}/review/',
            data=json.dumps({'action': 'cash_transfer', 'source_cash_account_id': own_card.id}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(proposed.status_code, 400)
        self.assertIn('лише рахунок «Готівка»', proposed.json()['error'])

    def test_terminal_cash_transfer_rejects_insufficient_cash(self):
        top_up = self._txn(
            Transaction.TYPE_INCOME, Decimal('1250'), account=self.account,
            comment='Поповнення через термінал mono',
        )
        top_up.external_data = {'counter_name': 'City24'}
        top_up.save(update_fields=['external_data'])
        self.client.force_login(self.user)
        proposed = self.client.post(
            f'/api/v2/terminal-cash/candidates/{top_up.id}/review/',
            data=json.dumps({'action': 'cash_transfer', 'source_cash_account_id': self.cash.id}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(proposed.status_code, 400)
        self.assertIn('Недостатньо готівки', proposed.json()['error'])

    def test_known_viktor_refund_rule_is_strict(self):
        from .models import RecurrenceRule
        refund_category = Category.objects.create(company=self.company, name='Повернення коштів', type='income')
        viktor = Counterparty.objects.create(company=self.company, name='Віктор Викторович', type='other')
        rule = RecurrenceRule.objects.create(
            company=self.company, title='Повернення за оренду', frequency='monthly',
            start_date=timezone.localdate(), template_type='income',
            template_counterparty=viktor, template_category=refund_category,
            template_account=self.cash, template_comment='Повернення за оренду',
        )
        refund = self._txn(Transaction.TYPE_INCOME, Decimal('12000'), account=self.cash,
                           comment='Повернення за оренду')
        refund.counterparty = viktor
        refund.category = refund_category
        refund.recurrence_rule = rule
        refund.source = 'recurring'
        refund.save(update_fields=['counterparty', 'category', 'recurrence_rule', 'source'])
        ledger_v2.classify_known_rent_refund(refund, user=self.user)
        refund.refresh_from_db()
        self.assertEqual(refund.economic_kind, 'expense_refund')
        self.assertTrue(refund.is_business)

        terminal = self._txn(Transaction.TYPE_INCOME, Decimal('19820'), account=self.account,
                             comment='City24')
        terminal.counterparty = viktor
        terminal.category = refund_category
        terminal.recurrence_rule = rule
        terminal.source = 'integration'
        terminal.external_id = 'bank-190'
        terminal.save(update_fields=['counterparty', 'category', 'recurrence_rule', 'source', 'external_id'])
        self.assertIsNone(ledger_v2.classify_known_rent_refund(terminal, user=self.user))
        terminal.refresh_from_db()
        self.assertEqual(terminal.economic_kind, 'unknown')

    def test_terminal_review_command_only_creates_decision_markers(self):
        top_up = self._txn(
            Transaction.TYPE_INCOME, Decimal('1250'), account=self.account,
            comment='City24 поповнення',
        )
        dry_run = StringIO()
        call_command('finance_prepare_terminal_reviews', stdout=dry_run)
        self.assertIn(str(top_up.id), dry_run.getvalue())
        self.assertFalse(top_up.classification_reviews.exists())
        call_command('finance_prepare_terminal_reviews', '--apply', stdout=StringIO())
        top_up.refresh_from_db()
        review = top_up.classification_reviews.get(status='pending')
        self.assertEqual(review.proposal['kind'], 'terminal_cash_decision')
        self.assertEqual(top_up.type, Transaction.TYPE_INCOME)

    def test_tax_components_and_payment_intent_fallback(self):
        group = ObligationGroup.objects.create(company=self.company, title='Налоги', counterparty=self.cp)
        military = ObligationComponent.objects.create(group=group, name='Военный сбор',
                                                       fixed_amount=Decimal('865'), recipient_card=self.card_a,
                                                       payment_purpose_template='Военный сбор за месяц')
        unified = ObligationComponent.objects.create(group=group, name='Единый налог',
                                                      fixed_amount=Decimal('1729'), recipient_card=self.card_b)
        payment = self._txn(Transaction.TYPE_EXPENSE, Decimal('865'))
        settlement = ObligationSettlement.objects.create(company=self.company, payment=payment,
                                                         amount=Decimal('865'), currency='UAH')
        obligations_v2.allocate_settlement(settlement=settlement, allocations={str(military.id): '865'})
        self.assertEqual(obligations_v2.group_summary(group)['status'], 'partial')
        intent, _ = payment_intents.create_intent(company=self.company, account=self.account,
                                                  amount=Decimal('1729'), component=unified,
                                                  recipient_card=self.card_b, user=self.user)
        payment_intents.transition(intent, 'awaiting_confirmation', user=self.user)
        payload = payment_intents.instruction_payload(intent)
        self.assertFalse(payload['capability']['outgoing_supported'])
        self.assertEqual(payload['recipient']['iban'], 'UA222')

    def test_refund_link_is_not_a_new_sale(self):
        expense = self._txn(Transaction.TYPE_EXPENSE, Decimal('12000'))
        refund = self._txn(Transaction.TYPE_INCOME, Decimal('2000'))
        link = ledger_v2.link_refund(refund_transaction=refund, original_transaction=expense,
                                     amount=Decimal('2000'), user=self.user)
        refund.refresh_from_db()
        self.assertEqual(link.original_transaction_id, expense.id)
        self.assertEqual(refund.economic_kind, 'expense_refund')

    def test_v2_api_creates_group_component_and_intent(self):
        self.client.force_login(self.user)
        group_response = self.client.post(
            '/api/v2/obligation-groups/',
            data=json.dumps({'title': 'Налоги API', 'counterparty_id': self.cp.id}),
            content_type='application/json',
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(group_response.status_code, 201)
        group_id = group_response.json()['group']['id']
        group_detail = self.client.get(
            f'/api/v2/obligation-groups/{group_id}/',
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(group_detail.status_code, 200)
        self.assertEqual(group_detail.json()['group']['remaining'], '0')
        component_response = self.client.post(
            f'/api/v2/obligation-groups/{group_id}/components/',
            data=json.dumps({'name': 'Военный сбор', 'fixed_amount': '865',
                             'recipient_card_id': self.card_a.id,
                             'payment_purpose_template': 'Военный сбор'}),
            content_type='application/json',
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(component_response.status_code, 201)
        component_id = component_response.json()['component']['id']
        intent_response = self.client.post(
            f'/api/v2/obligation-components/{component_id}/payment-intent/',
            data=json.dumps({'account_id': self.account.id, 'amount': '865'}),
            content_type='application/json',
            HTTP_HOST='fin.twocomms.shop',
        )
        self.assertEqual(intent_response.status_code, 200)
        self.assertEqual(intent_response.json()['intent']['status'], 'awaiting_confirmation')

    def test_statement_matching_confirms_the_first_pending_intent(self):
        intent, _ = payment_intents.create_intent(
            company=self.company, account=self.account, amount=Decimal('865'),
            recipient_card=self.card_a, user=self.user,
        )
        payment_intents.transition(intent, 'awaiting_confirmation', user=self.user)
        payment_intents.transition(intent, 'submitted', user=self.user)
        statement = self._txn(Transaction.TYPE_EXPENSE, Decimal('865'))
        statement.external_data = {'counterIban': 'UA111'}
        statement.save(update_fields=['external_data'])
        matches = payment_intents.match_pending_intents(statement, user=self.user)
        intent.refresh_from_db()
        self.assertEqual(len(matches), 1)
        self.assertEqual(intent.status, 'detected')
