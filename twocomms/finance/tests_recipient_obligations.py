import datetime as dt
import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (Account, Company, Counterparty, CounterpartyAlias, CounterpartyCard,
                     ObligationComponent, ObligationGroup, ObligationSettlement, RecurrenceRule, Transaction, get_default_company)
from .services import cards, mono, obligations_v2, payables, transactions


@override_settings(ALLOWED_HOSTS=['testserver','fin.twocomms.shop'], SECURE_SSL_REDIRECT=False)
class RecipientObligationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('recipient', '', 'x')
        self.company = get_default_company()
        self.account = Account.objects.create(company=self.company, name='Чорна', currency='UAH')
        self.cp = Counterparty.objects.create(company=self.company, name='Влада мама')
        self.other = Counterparty.objects.create(company=self.company, name='Інша людина')
        self.alias = CounterpartyAlias.objects.create(company=self.company, counterparty=self.cp,
            value='Олена Д.', normalized=cards.normalize_alias('Олена Д.'))
        self.group = ObligationGroup.objects.create(company=self.company, title='Квартира', counterparty=self.cp)
        self.rent = ObligationComponent.objects.create(group=self.group, name='Квартплата', fixed_amount=4000)
        self.utility = ObligationComponent.objects.create(group=self.group, name='Комунальні')
        self.when = timezone.make_aware(dt.datetime(2026,11,10,20))
        self.client.force_login(self.user)

    def payment(self, amount='1337.17', **kwargs):
        return transactions.create_transaction(user=self.user, type='expense', account=self.account,
            amount=Decimal(amount), date_actual=kwargs.pop('date_actual', self.when),
            comment=kwargs.pop('comment','Олена Д.'), **kwargs)

    def get_review(self, txn, period=None):
        return self.client.get(f'/api/v2/transactions/{txn.id}/obligation-payment-review/',
            {'period_key':period} if period else {}, HTTP_HOST='fin.twocomms.shop')

    def settle(self, txn, component, *, period='2026-11', planned=None, amount=None):
        return self.client.post(f'/api/v2/obligation-components/{component.id}/settle-existing/',
            data=json.dumps({'transaction_id':txn.id, 'amount':str(amount or txn.amount),
                'period_key':period, 'planned_txn_id':planned.id if planned else None, 'confirm_expense':True}),
            content_type='application/json', HTTP_HOST='fin.twocomms.shop')

    def test_confirmed_alias_assigns_identity_but_requires_purpose_decision(self):
        txn = self.payment(comment='ОЛЕНА Д')
        self.assertEqual(txn.counterparty_id,self.cp.id)
        self.assertFalse(txn.settlements.exists())
        self.assertEqual(txn.economic_kind,'unknown')
        review = txn.classification_reviews.get(proposal__kind='obligation_payment')
        self.assertEqual(review.status,'pending')
        data = self.get_review(txn).json()['review']
        self.assertEqual(data['counterparty']['name'],'Влада мама')
        self.assertEqual(data['period_key'],'2026-11')
        self.assertEqual({r['name'] for r in data['options']},{'Квартплата','Комунальні'})

    def test_alias_is_exact_active_and_does_not_override_explicit_person(self):
        self.assertIsNone(self.payment(comment='Олена Д. інша людина').counterparty_id)
        self.assertEqual(self.payment(counterparty=self.other).counterparty_id,self.other.id)
        self.alias.is_active=False; self.alias.save()
        self.assertIsNone(self.payment().counterparty_id)

    def test_alias_from_another_company_is_not_recognized(self):
        self.alias.is_active=False; self.alias.save()
        company=Company.objects.create(name='Інша компанія')
        person=Counterparty.objects.create(company=company,name='Інший контрагент')
        CounterpartyAlias.objects.create(company=company,counterparty=person,value='Олена Д.',normalized='оленад')
        self.assertIsNone(self.payment().counterparty_id)

    def test_saved_mask_assigns_person_but_last_four_does_not(self):
        CounterpartyCard.objects.create(company=self.company,counterparty=self.cp,pan_mask='414949****7490',last4='7490')
        exact=self.payment(comment='414949****7490')
        weak=self.payment(comment='Переказ 7490')
        self.assertEqual(exact.counterparty_id,self.cp.id)
        self.assertIsNone(weak.counterparty_id)
        self.assertEqual(cards.match_counterparty_by_payment(self.company,weak)['candidates'],[self.cp])

    def test_ambiguous_saved_mask_never_assigns_automatically(self):
        for person in (self.cp,self.other):
            CounterpartyCard.objects.create(company=self.company,counterparty=person,pan_mask='414949****7490',last4='7490')
        txn=self.payment(comment='414949****7490')
        self.assertIsNone(txn.counterparty_id)
        self.assertEqual(len(cards.match_counterparty_by_payment(self.company,txn)['candidates']),2)

    def test_saved_counterparty_iban_recognizes_recipient_without_a_card(self):
        self.cp.iban='UA_TEST_RECIPIENT'; self.cp.save()
        txn=self.payment(comment='Платіж',external_data={'counter_iban':'UA_TEST_RECIPIENT'})
        self.assertEqual(txn.counterparty_id,self.cp.id)

    def test_new_bank_import_recognizes_recipient_and_does_not_settle(self):
        txn=mono._import_item(self.account,{'id':'future-rent','time':int(self.when.timestamp()),
            'amount':-400000,'description':'Олена Д.','mcc':4829,'currencyCode':980},user=self.user)
        self.assertEqual(txn.counterparty_id,self.cp.id)
        self.assertEqual(txn.amount,Decimal('4000'))
        self.assertFalse(txn.settlements.exists())
        self.assertEqual(txn.classification_reviews.filter(proposal__kind='obligation_payment').count(),1)
        self.assertIsNone(mono._import_item(self.account,{'id':'future-rent','time':int(self.when.timestamp()),
            'amount':-400000,'description':'Олена Д.'},user=self.user))
        self.assertEqual(txn.classification_reviews.count(),1)

    def test_confirming_two_components_closes_month_and_preserves_actual_balances(self):
        utility=self.payment(); rent=self.payment('4000')
        self.account.refresh_from_db(); balance=self.account.current_balance
        self.assertEqual(self.settle(utility,self.utility).status_code,200)
        self.assertEqual(obligations_v2.group_summary(self.group,period_key='2026-11')['status'],'partial')
        self.assertEqual(self.settle(rent,self.rent).status_code,200)
        self.assertEqual(obligations_v2.group_summary(self.group,period_key='2026-11')['status'],'paid')
        self.account.refresh_from_db(); self.assertEqual(self.account.current_balance,balance)
        for txn,component in ((utility,self.utility),(rent,self.rent)):
            txn.refresh_from_db()
            self.assertEqual(txn.economic_kind,'operating_expense')
            review=txn.classification_reviews.get(proposal__kind='obligation_payment')
            self.assertEqual(review.status,'accepted')
            self.assertEqual(review.proposal['component_id'],component.id)
            self.assertEqual(review.reviewed_by_id,self.user.id)
            self.assertIsNone(self.get_review(txn).json()['review'])
        december=self.payment(date_actual=timezone.make_aware(dt.datetime(2026,12,10,12)))
        self.assertEqual(len(self.get_review(december).json()['review']['options']),2)

    def test_rejecting_purpose_keeps_payment_and_does_not_recreate_prompt(self):
        txn=self.payment(); review=txn.classification_reviews.get(proposal__kind='obligation_payment')
        resp=self.client.post(f'/api/v2/reviews/{review.id}/action/',data=json.dumps({'action':'reject'}),
            content_type='application/json',HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(resp.status_code,200)
        self.assertIsNone(self.get_review(txn).json()['review'])
        self.assertFalse(txn.settlements.exists())
        txn.refresh_from_db(); self.assertEqual(txn.amount,Decimal('1337.17'))

    def test_generic_accept_cannot_bypass_component_selection(self):
        txn=self.payment(); review=txn.classification_reviews.get(proposal__kind='obligation_payment')
        resp=self.client.post(f'/api/v2/reviews/{review.id}/action/',data=json.dumps({'action':'accept'}),
            content_type='application/json',HTTP_HOST='fin.twocomms.shop')
        self.assertEqual(resp.status_code,400)
        review.refresh_from_db(); self.assertEqual(review.status,'pending')
        self.assertFalse(txn.settlements.exists())

    def test_wrong_person_and_stale_amount_leave_no_links(self):
        wrong=self.payment(counterparty=self.other)
        self.assertEqual(self.settle(wrong,self.utility).status_code,400)
        txn=self.payment()
        self.assertEqual(self.settle(txn,self.utility,amount='1000').status_code,400)
        self.assertFalse(ObligationSettlement.objects.exists())

    def test_allocator_failure_rolls_back_identity_category_and_link(self):
        txn=self.payment(comment='Невідомо')
        with patch('finance.views.finance_v2.obligations_v2.allocate_settlement',side_effect=ValueError('failed')):
            self.assertEqual(self.settle(txn,self.utility).status_code,400)
        txn.refresh_from_db()
        self.assertIsNone(txn.counterparty_id)
        self.assertFalse(ObligationSettlement.objects.exists())

    def test_recipient_payments_rank_before_other_recent_payments(self):
        old=self.payment(date_actual=self.when-dt.timedelta(days=365))
        for n in range(205):
            Transaction.objects.create(company=self.company,type='expense',status='actual',amount=100,
                amount_base=100,currency='UAH',account=self.account,date_actual=self.when,comment=f'Інше {n}')
        candidates=payables.payable_candidates(self.company,ttype='expense',counterparty=self.cp,limit=5)
        self.assertEqual(candidates[0]['id'],old.id)

    def test_unassigned_saved_card_is_prioritized_without_rewriting_history(self):
        CounterpartyCard.objects.create(company=self.company,counterparty=self.cp,pan_mask='414949****7490',last4='7490')
        txn=Transaction.objects.create(company=self.company,type='expense',status='actual',amount=100,
            amount_base=100,currency='UAH',account=self.account,date_actual=self.when,comment='414949****7490')
        self.payment(comment='Інше')
        self.assertEqual(payables.payable_candidates(self.company,ttype='expense',counterparty=self.cp)[0]['id'],txn.id)
        txn.refresh_from_db(); self.assertIsNone(txn.counterparty_id)

    def test_period_bounds_include_local_midnight_without_database_timezone_tables(self):
        start,end=obligations_v2.payment_period_bounds('2026-11')
        self.assertEqual(timezone.localtime(start).date(),dt.date(2026,11,1))
        self.assertEqual(timezone.localtime(end).date(),dt.date(2026,12,1))
        rule=RecurrenceRule.objects.create(company=self.company,frequency='monthly',start_date=dt.date(2026,11,1),
            template_type='expense',template_amount=Decimal('4000'),template_account=self.account,
            template_counterparty=self.cp,title='Квартира')
        self.group.recurrence_rule=rule; self.group.save()
        plan=Transaction.objects.create(company=self.company,type='expense',status='planned',amount=4000,
            amount_base=4000,currency='UAH',account=self.account,date_actual=start,recurrence_rule=rule,counterparty=self.cp)
        data=self.get_review(self.payment()).json()['review']
        self.assertTrue(all(r['planned_txn_id']==plan.id for r in data['options']))

    def test_journal_exposes_pending_confirmation_badge(self):
        txn=self.payment()
        resp=self.client.get('/',HTTP_HOST='fin.twocomms.shop')
        self.assertContains(resp,'Підтвердити витрату')
        self.assertContains(resp,'Влада мама')
        self.assertFalse(txn.settlements.exists())

    def test_bank_alias_search_finds_the_person_and_their_payments(self):
        CounterpartyAlias.objects.create(company=self.company,counterparty=self.cp,value='OLND',normalized='olnd')
        txn=self.payment()
        response=self.client.get('/',{'search':'OLND'},HTTP_HOST='fin.twocomms.shop')
        self.assertContains(response,'Влада мама')
        self.assertContains(response,f'data-txn-id="{txn.id}"')
        response=self.client.get('/counterparties/',{'search':'OLND'},HTTP_HOST='fin.twocomms.shop')
        self.assertContains(response,'Влада мама')
        response=self.client.get(f'/counterparties/{self.cp.id}/',HTTP_HOST='fin.twocomms.shop')
        self.assertContains(response,'Імена у виписці:')
        self.assertContains(response,'OLND')
