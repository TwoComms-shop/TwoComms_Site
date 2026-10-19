from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PointsHistory, UserPoints
from orders.models import Order, PaymentAttempt
from orders.promo_reservations import (
    PromoReservationError, consume_payment_attempt_promo,
    release_payment_attempt_promo, reserve_promos_for_checkout,
)
from storefront.models import PromoCode, PromoCodeGroup, PromoCodeGuestUsage, PromoCodeUsage, UserPromoCode
from storefront.services.loyalty_rewards import LoyaltyRewardError, redeem_points_reward
from storefront.services.promo_composition import compose_promos


class PointsRewardConversionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='points-conversion')
        self.balance, _ = UserPoints.objects.update_or_create(user=self.user, defaults={'points': 200, 'total_earned': 200})

    def test_operation_replay_and_second_exchange_issue_independent_rights(self):
        first = redeem_points_reward(user=self.user, operation_id='exchange-one')
        replay = redeem_points_reward(user=self.user, operation_id='exchange-one')
        second = redeem_points_reward(user=self.user, operation_id='exchange-two')
        self.balance.refresh_from_db()
        self.assertEqual(first.pk, replay.pk)
        self.assertNotEqual(first.promo_code_id, second.promo_code_id)
        self.assertEqual((self.balance.points, self.balance.total_spent), (0, 200))
        self.assertEqual(PointsHistory.objects.filter(user=self.user, points_change=-100).count(), 2)
        self.assertEqual(first.source, 'points_reward')
        self.assertEqual(first.promo_code.max_uses, 1)
        self.assertEqual(first.promo_code.discount_value, 10)
        self.assertTrue(first.promo_code.one_time_per_user)
        self.assertIsNone(first.expires_at)
        self.assertIsNone(first.promo_code.valid_until)

    def test_grant_failure_rolls_back_code_and_debit(self):
        with patch.object(UserPromoCode.objects, 'create', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError):
                redeem_points_reward(user=self.user, operation_id='failed')
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.points, 200)
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(PointsHistory.objects.exists())

    def test_history_failure_rolls_back_grant_and_debit(self):
        with patch.object(PointsHistory.objects, 'create', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError):
                redeem_points_reward(user=self.user, operation_id='failed-history')
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.points, self.balance.total_spent), (200, 0))
        self.assertFalse(PromoCode.objects.exists())
        self.assertFalse(UserPromoCode.objects.exists())

    def test_insufficient_points_creates_nothing(self):
        UserPoints.objects.filter(pk=self.balance.pk).update(points=99)
        with self.assertRaises(LoyaltyRewardError):
            redeem_points_reward(user=self.user, operation_id='too-poor')
        self.assertFalse(PromoCode.objects.exists())

    def test_foreign_account_cannot_redeem_private_points_code(self):
        grant = redeem_points_reward(user=self.user, operation_id='private')
        stranger = get_user_model().objects.create_user(username='points-stranger')
        with self.assertRaisesRegex(PromoReservationError, 'owner_required'):
            reserve_promos_for_checkout(codes=[grant.promo_code.code], user=stranger, total_amount=1000)
        grant.promo_code.refresh_from_db()
        self.assertEqual(grant.promo_code.current_uses, 0)

    def test_native_view_has_real_code_and_replays_without_debit(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('buy_with_points'))
        operation = response.context['purchase_operation_id']
        payload = {'item_id': 'promo_10', 'purchase_operation_id': operation}
        self.client.post(reverse('purchase_with_points'), payload)
        self.client.post(reverse('purchase_with_points'), payload)
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.points, 100)
        self.assertEqual(UserPromoCode.objects.count(), 1)
        self.assertNotEqual(UserPromoCode.objects.get().promo_code.code, 'POINTS10')
        self.assertContains(response, 'name="purchase_operation_id"')

    def test_native_view_rejects_missing_operation(self):
        self.client.force_login(self.user)
        self.client.post(reverse('purchase_with_points'), {'item_id': 'promo_10'})
        self.balance.refresh_from_db()
        self.assertEqual(self.balance.points, 200)

    def test_stale_donation_writer_cannot_overwrite_reward_debit(self):
        stale = UserPoints.objects.get(pk=self.balance.pk)
        redeem_points_reward(user=self.user, operation_id='before-donation')
        self.assertFalse(stale.spend_points(200, 'stale donation'))
        self.assertEqual(stale.points, 100)
        self.assertTrue(stale.spend_points(100, 'confirmed donation'))
        stale.refresh_from_db()
        self.assertEqual((stale.points, stale.total_spent), (0, 200))
        self.assertEqual(PointsHistory.objects.filter(user=self.user, change_type='spent').count(), 2)

    def test_stale_earning_writer_preserves_reward_debit_and_spent_total(self):
        stale = UserPoints.objects.get(pk=self.balance.pk)
        redeem_points_reward(user=self.user, operation_id='before-earned')
        stale.add_points(50, 'delivered order')
        stale.refresh_from_db()
        self.assertEqual((stale.points, stale.total_spent, stale.total_earned), (150, 100, 250))
        self.assertEqual(PointsHistory.objects.filter(user=self.user).first().balance_after, 150)

    def test_earning_history_failure_rolls_back_balance(self):
        with patch.object(PointsHistory.objects, 'create', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError):
                self.balance.add_points(50, 'failed earning')
        self.balance.refresh_from_db()
        self.assertEqual((self.balance.points, self.balance.total_earned), (200, 200))


class MultiPromoConversionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='multi-promo-owner')

    def promo(self, code, value, kind='percentage', **kwargs):
        return PromoCode.objects.create(code=code, discount_type=kind, discount_value=value, **kwargs)

    def attempt(self, batch):
        return PaymentAttempt.objects.create(
            fingerprint='multi-invoice', user=self.user, full_name='Buyer', phone='+380501112233',
            city='Kyiv', np_office='Branch 1', gross_amount=1000, payable_amount=1000 - batch.discount,
            payment_amount=1000 - batch.discount, discount_amount=batch.discount,
            promo_code=batch.promo, event_state=batch.event_state,
        )

    def test_fixed_then_sequential_percentage_and_entry_order_independence(self):
        p1 = self.promo('TEN1', 10)
        fixed = self.promo('FIXED200', 200, 'fixed')
        p2 = self.promo('TEN2', 10)
        for promos in ([p1, fixed, p2], [p2, p1, fixed]):
            result = compose_promos(total_amount=1000, promos=promos)
            self.assertEqual((result.total, result.discount), (Decimal('648.00'), Decimal('352.00')))
            self.assertEqual(sum(amount for _, amount in result.contributions), result.discount)
        self.assertEqual(compose_promos(total_amount=1000, promos=[p1, p2]).discount, Decimal('190.00'))

    def test_expired_secondary_rolls_back_first_reservation(self):
        first = self.promo('FIRST', 10, max_uses=1)
        expired = self.promo('EXPIRED', 10, valid_until=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(PromoReservationError):
            reserve_promos_for_checkout(codes=[first.code, expired.code], user=self.user, total_amount=1000)
        first.refresh_from_db()
        self.assertEqual(first.current_uses, 0)

    def test_duplicate_and_account_group_cannot_double_discount(self):
        group = PromoCodeGroup.objects.create(name='One account right', one_per_account=True)
        one = self.promo('GROUP1', 10, group=group)
        two = self.promo('GROUP2', 10, group=group)
        for codes in ([one.code, one.code.lower()], [one.code, two.code]):
            with self.assertRaises(PromoReservationError):
                reserve_promos_for_checkout(codes=codes, user=self.user, total_amount=1000)
        one.refresh_from_db()
        self.assertEqual(one.current_uses, 0)

    def test_zero_contribution_keeps_capacity(self):
        fixed = self.promo('FULLVALUE', 1000, 'fixed', max_uses=1)
        percent = self.promo('UNNEEDED', 10, max_uses=1)
        result = reserve_promos_for_checkout(codes=[percent.code, fixed.code], user=self.user, total_amount=1000)
        self.assertEqual(result.discount, 1000)
        self.assertEqual([p.pk for p in result.promos], [fixed.pk])
        percent.refresh_from_db()
        self.assertEqual(percent.current_uses, 0)

    def test_secondary_pending_reservation_blocks_other_invoice(self):
        first = self.promo('MULTIFIRST', 10)
        secondary = self.promo('SECONDARY', 10, one_time_per_user=True)
        batch = reserve_promos_for_checkout(codes=[first.code, secondary.code], user=self.user, total_amount=1000)
        self.attempt(batch)
        with self.assertRaisesRegex(PromoReservationError, 'already_reserved_or_used'):
            reserve_promos_for_checkout(codes=[secondary.code], user=self.user, total_amount=1000)

    def test_release_all_components_is_idempotent(self):
        promos = [self.promo('RELEASE1', 10), self.promo('RELEASE2', 10)]
        batch = reserve_promos_for_checkout(promo_ids=[p.pk for p in promos], user=self.user, total_amount=1000)
        attempt = self.attempt(batch)
        self.assertTrue(release_payment_attempt_promo(attempt))
        self.assertFalse(release_payment_attempt_promo(attempt))
        self.assertEqual(list(PromoCode.objects.values_list('current_uses', flat=True)), [0, 0])

    def test_consume_records_each_code_and_replay_is_idempotent(self):
        promos = [self.promo('CONSUME1', 10), self.promo('CONSUME2', 10)]
        batch = reserve_promos_for_checkout(promo_ids=[p.pk for p in promos], user=self.user, total_amount=1000)
        attempt = self.attempt(batch)
        order = Order.objects.create(user=self.user, full_name='Buyer', phone='+380501112233',
                                     city='Kyiv', np_office='Branch 1', total_sum=810)
        self.assertTrue(consume_payment_attempt_promo(attempt, order=order))
        self.assertTrue(consume_payment_attempt_promo(attempt, order=order))
        self.assertEqual(PromoCodeUsage.objects.filter(order=order).count(), 2)
        self.assertEqual(list(PromoCode.objects.values_list('current_uses', flat=True)), [1, 1])

    def test_private_grant_expiry_is_preserved(self):
        promo = self.promo('EXPIREDGRANT', 10)
        UserPromoCode.objects.create(user=self.user, promo_code=promo, survey_key='expired-right',
                                    expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesRegex(PromoReservationError, 'owner_required'):
            reserve_promos_for_checkout(codes=[promo.code], user=self.user, total_amount=1000)

    def test_released_batch_cannot_consume_capacity_reissued_elsewhere(self):
        promos = [self.promo('STALE1', 10), self.promo('STALE2', 10)]
        batch = reserve_promos_for_checkout(promo_ids=[p.pk for p in promos], user=self.user, total_amount=1000)
        attempt = self.attempt(batch)
        release_payment_attempt_promo(attempt)
        reserve_promos_for_checkout(promo_ids=[p.pk for p in promos], user=self.user, total_amount=1000)
        order = Order.objects.create(user=self.user, full_name='Buyer', phone='+380501112233',
                                     city='Kyiv', np_office='Branch 1', total_sum=810)
        self.assertFalse(consume_payment_attempt_promo(attempt, order=order))
        self.assertFalse(PromoCodeUsage.objects.filter(order=order).exists())
        self.assertEqual(list(PromoCode.objects.values_list('current_uses', flat=True)), [1, 1])

    def test_secondary_persistence_failure_does_not_retry_primary_usage(self):
        promos = [self.promo('RETRY1', 10), self.promo('RETRY2', 10)]
        batch = reserve_promos_for_checkout(promo_ids=[p.pk for p in promos], user=self.user, total_amount=1000)
        attempt = self.attempt(batch)
        order = Order.objects.create(user=self.user, full_name='Buyer', phone='+380501112233',
                                     city='Kyiv', np_office='Branch 1', total_sum=810)
        create_usage = PromoCodeUsage.objects.create
        def intermittent_failure(**kwargs):
            if kwargs['promo_code'].pk == promos[1].pk:
                raise RuntimeError('secondary usage storage failed')
            return create_usage(**kwargs)
        with patch.object(PromoCodeUsage.objects, 'create', side_effect=intermittent_failure):
            self.assertFalse(consume_payment_attempt_promo(attempt, order=order))
        self.assertEqual(attempt.event_state['promo_reservations'][0]['state'], 'consumed')
        self.assertEqual(attempt.event_state['promo_reservations'][1]['state'], 'reserved')
        self.assertTrue(consume_payment_attempt_promo(attempt, order=order))
        self.assertEqual(PromoCodeUsage.objects.filter(order=order).count(), 2)

    def test_existing_guest_ugc_capabilities_stack_and_consume_separately(self):
        from storefront.tests.test_ugc_guest_promo import _attach_external_ugc_reward
        expiry = timezone.now() + timedelta(days=90)
        promos = [self.promo('UGCBATCH1', 10, guest_redeemable=True, max_uses=1, valid_until=expiry),
                  self.promo('UGCBATCH2', 10, guest_redeemable=True, max_uses=1, valid_until=expiry)]
        for index, promo in enumerate(promos):
            _attach_external_ugc_reward(promo, suffix=f'batch-{index}')
        batch = reserve_promos_for_checkout(codes=[p.code for p in promos], user=None, total_amount=1000)
        self.assertEqual(batch.discount, 190)
        attempt = self.attempt(batch)
        attempt.user = None
        attempt.save(update_fields=['user'])
        order = Order.objects.create(full_name='Guest', phone='+380501112233',
                                     city='Kyiv', np_office='Branch 1', total_sum=810)
        self.assertTrue(consume_payment_attempt_promo(attempt, order=order))
        self.assertEqual(PromoCodeGuestUsage.objects.filter(order=order, state='consumed').count(), 2)
        for promo in promos:
            promo.refresh_from_db()
            self.assertEqual(promo.valid_until, expiry)
