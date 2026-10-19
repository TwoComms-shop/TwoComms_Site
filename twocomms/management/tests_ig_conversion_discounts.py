from django.test import TestCase
from django.utils import timezone

from management.models import IgClient, IgFollowUpTask, InstagramBotMessage
from management.services.bot_followups import discount_recommendation_reason, next_discount_percent


class DiscountRecommendationConversionTests(TestCase):
    def setUp(self):
        self.buyer = IgClient.get_or_create_for_sender('conversion-budget-case')
        self.buyer.followup_level = 2
        self.buyer.primary_objection = IgClient.Objection.PRICE
        self.buyer.last_user_message_at = timezone.now()
        self.buyer.save()

    def say(self, text, role=InstagramBotMessage.Role.USER):
        return InstagramBotMessage.objects.create(client=self.buyer, sender_id='conversion-budget-case',
                                                  role=role, text=text, status='done')

    def test_silence_derived_objection_and_caller_flag_are_not_evidence(self):
        self.assertEqual(next_discount_percent(self.buyer, explicit_negotiation=True), 0)

    def test_price_question_thinking_and_quoted_budget_are_not_personal_requests(self):
        for text in ('Яка ціна?', 'Я подумаю', 'дорого', 'Ви написали «для мене це дорого»?',
                     'Це не дорого, оформлюємо', 'Dear friend, price?', 'Не потрібна знижка',
                     "Ви написали 'для мене це дорого'?", 'Раніше я не міг собі дозволити',
                     'Я військовий, можете зробити знижку на звичайну футболку?'):
            with self.subTest(text=text):
                self.say(text)
                self.assertEqual(next_discount_percent(self.buyer, explicit_negotiation=True), 0)

    def test_first_message_clear_budget_case_can_be_recommended(self):
        message = self.say('Не можу собі дозволити за таку суму')
        self.assertEqual(discount_recommendation_reason(self.buyer), ('customer_budget_barrier', message.pk))
        self.assertEqual(next_discount_percent(self.buyer), 5)
        self.assertEqual(next_discount_percent(self.buyer, explicit_negotiation=True), 10)

    def test_model_copy_is_not_customer_evidence(self):
        self.say('Для мене це дорого', role=InstagramBotMessage.Role.MODEL)
        self.assertEqual(next_discount_percent(self.buyer, explicit_negotiation=True), 0)

    def test_current_message_replaces_earlier_budget_case(self):
        self.say('Can you give me a discount?')
        self.assertEqual(next_discount_percent(self.buyer, explicit_negotiation=True), 10)
        self.say('Так, беру за повну ціну')
        self.assertEqual(next_discount_percent(self.buyer, explicit_negotiation=True), 0)
