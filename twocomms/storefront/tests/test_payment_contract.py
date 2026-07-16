from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from accounts.models import UserProfile
from accounts.payment import PAY_TYPE_CHOICES, normalize_pay_type
from storefront.views.auth import ProfileSetupForm


class PaymentTypeNormalizerTests(SimpleTestCase):
    def test_legacy_and_canonical_values_share_one_contract(self):
        expected = {
            "full": "online_full",
            "online_full": "online_full",
            "partial": "prepay_200",
            "prepay_200": "prepay_200",
            "cod": "cod",
            "cash": "cod",
        }

        self.assertEqual(
            {value: normalize_pay_type(value) for value in expected},
            expected,
        )

    def test_strict_normalization_rejects_unsupported_values(self):
        with self.assertRaises(ValueError):
            normalize_pay_type("cash-on-a-random-card", default=None)

    def test_profile_form_renders_canonical_choices_and_accepts_legacy_posts(self):
        self.assertEqual(tuple(ProfileSetupForm.base_fields["pay_type"].choices), PAY_TYPE_CHOICES)

        form = ProfileSetupForm(data={"phone": "+380991234567", "pay_type": "partial"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["pay_type"], "prepay_200")


class UserProfilePaymentTypeTests(TestCase):
    def test_profile_field_matches_shared_canonical_choices(self):
        field = UserProfile._meta.get_field("pay_type")

        self.assertEqual(field.max_length, 20)
        self.assertEqual(tuple(field.choices), PAY_TYPE_CHOICES)
        self.assertEqual(field.default, "online_full")

    def test_every_supported_value_saves_and_round_trips(self):
        user = User.objects.create_user(username="payment-contract-user")
        profile = user.userprofile

        for pay_type, _label in PAY_TYPE_CHOICES:
            with self.subTest(pay_type=pay_type):
                profile.pay_type = pay_type
                profile.full_clean()
                profile.save(update_fields=["pay_type"])
                profile.refresh_from_db()
                self.assertEqual(profile.pay_type, pay_type)
