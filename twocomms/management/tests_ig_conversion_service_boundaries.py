"""Owner refusal terms and manager-owned service facts remain separate money domains."""
from copy import deepcopy
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from management.models import IgClient, IgOrderAttribution, IgPaymentEvent, IgPostSaleCase, InstagramBotMessage
from management.services.ig_post_sale import open_post_sale_case
from management.services.ig_reply_truth import ReplyTruthContext, validate_reply_truth
from orders.models import Order


@override_settings(ROOT_URLCONF='twocomms.urls_management')
class RefusalServiceMoneyBoundaryTests(TestCase):
    def setUp(self):
        self.manager = get_user_model().objects.create_superuser('conversion-service-manager',
            email='conversion-service-manager@example.test', password='test')
        self.client.force_login(self.manager)
        self.customer = IgClient.get_or_create_for_sender('conversion-service-refusal')
        self.order = Order.objects.create(full_name='Ordinary refusal buyer', phone='0500000000',
            city='Київ', np_office='1', total_sum='950', status='ship', pay_type='prepay_200',
            payment_status='prepaid', payment_payload={'manager_confirmed_amount': '200.00',
                'manager_verification_scope': 'prepayment'})
        IgOrderAttribution.objects.create(order=self.order, client=self.customer,
            creation_mode='linked_existing', payment_source='manager_verified')

    def source(self, text):
        return InstagramBotMessage.objects.create(client=self.customer, sender_id=self.customer.igsid,
            role='user', status='done', text=text)

    def test_manager_records_all_200_retained_separately_from_150_cost_without_50_refund(self):
        source = self.source('Хочу повернення: відмовився від звичайної посилки після огляду. '
            'Передоплата 200 грн, доставка туди й назад 150 грн. Повернете 50 грн?')
        case = open_post_sale_case(self.customer, source, order=self.order)
        self.assertIsNotNone(case)
        self.assertEqual(case.case_type, 'return')
        payload_before = deepcopy(self.order.payment_payload)
        events_before = IgPaymentEvent.objects.count()
        note = 'Звичайна відмова після огляду: утримані всі 200 грн; фактична доставка 150 грн. '
        note += 'Це різні суми; автоматичного повернення різниці немає.'
        response = self.client.post(reverse('management_bot_post_sale_case_api', args=[self.customer.pk, case.pk]),
            {'order_id': self.order.pk, 'status': 'completed', 'manager_note': note,
             'carrier_cost': '150', 'refund_amount': '50'})
        self.assertEqual(response.status_code, 200, response.content)
        case.refresh_from_db()
        self.assertEqual(case.status, 'completed')
        self.assertEqual(case.manager_note, note)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'prepaid')
        self.assertEqual(self.order.payment_payload, payload_before)
        self.assertEqual(IgPaymentEvent.objects.count(), events_before)
        # Reported carrier expense and a case note grant no refund authority.
        truth = ReplyTruthContext(authorized_prices=(Decimal('950'), Decimal('200')),
            quoted_data=(source.text, note))
        self.assertFalse(validate_reply_truth('Повернемо вам 50 грн.', context=truth).valid)

    def test_current_payment_policy_does_not_derive_a_50_refund_and_keeps_defects_separate(self):
        from management.services.bot_knowledge import read_knowledge_manifest
        from management.services.ig_service_complaints import classify_service_complaint
        for locale, retained, defect in (
                ('uk', 'всі 200 грн не повертаються', 'окремий сервісний розгляд'),
                ('ru', 'все 200 грн не возвращаются', 'отдельное сервисное рассмотрение'),
                ('en', 'entire 200 UAH is retained', 'separate service review')):
            with self.subTest(locale=locale):
                body = read_knowledge_manifest(locale, topics=('payment',)).text
                self.assertIn(retained, body)
                self.assertIn(defect, body)
                self.assertNotIn('50', body)
        source = self.source('На моїй отриманій футболці брак, шов розійшовся. Хочу повернення.')
        complaint = classify_service_complaint(source.text)
        self.assertEqual(complaint['kind'], 'service_complaint')
        self.assertFalse(complaint['monetary_authority'])
        case = open_post_sale_case(self.customer, source, order=self.order)
        self.assertEqual(case.status, 'open')
        self.assertEqual(case.manager_note, '')
        self.assertIsNone(case.resolved_at)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'prepaid')


from management.tests_ig_restock_subscriptions import _RestockFixture


class ServiceThenIndependentKnownFactTests(_RestockFixture, TestCase):
    def test_native_fresh_product_question_answers_known_fact_with_old_service_case_still_open(self):
        from management.services.ig_product_inquiries import inquiry_reply, validate_inquiry_response
        from management.services.ig_response_control import ValidatedResponse
        from management.services.ig_response_plan import capture_response_plan
        from management.services.bot_followups import _has_open_service_conversation

        self.category.slug, self.category.name = 'tshirts', 'Футболки'
        self.category.save(update_fields=['slug', 'name'])
        self.canonical(f'Хочу https://twocomms.shop/product/{self.product.slug}/, '
            'розмір M, колір black, крій classic')
        service_source = self.source('Хочу повернення попередньої покупки')
        case = IgPostSaleCase.objects.create(client=self.client_row, source_message=service_source,
            case_type='return', status='open')
        source = self.source('Який склад цієї футболки?')
        revision, _token = self.revision(source)
        revision.client = self.client_row
        plan = capture_response_plan(self.client_row, revision=revision)
        information = plan.product_information
        self.assertEqual(information['status'], 'none', information)
        reply = inquiry_reply(information)
        self.assertIn('95%', reply)
        self.assertIn('5%', reply)
        response = ValidatedResponse(reply, ())
        self.assertEqual(validate_inquiry_response(information, response), '')
        self.assertTrue(validate_reply_truth(reply, context=plan.truth_context(ReplyTruthContext())).valid)
        material = [row['id'] for row in plan.obligations if row['kind'] == 'info:material']
        self.assertTrue(material, plan.obligations)
        coverage = plan.coverage(response)
        self.assertTrue(set(material) <= set(coverage['covered']))
        self.assertEqual(coverage['disposition'], 'complete', coverage)
        case.refresh_from_db()
        self.assertEqual(case.status, 'open')
        self.assertTrue(_has_open_service_conversation(self.client_row))
