from decimal import Decimal
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    Account, Category, Company, Counterparty, CounterpartyCard, FundingSource,
    ObligationComponent, ObligationGroup, ObligationSettlement, Transaction,
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
