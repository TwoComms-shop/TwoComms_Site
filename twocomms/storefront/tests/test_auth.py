"""Contract tests for storefront auth views."""

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from orders.nova_poshta_checkout import build_city_choice_token, build_warehouse_choice_token
from storefront.views.auth import LoginForm, RegisterForm


@override_settings(ALLOWED_HOSTS=["twocomms.shop", "testserver"])
class AuthViewTestCase(TestCase):
    host = "twocomms.shop"

    def setUp(self):
        super().setUp()
        self.client = Client(HTTP_HOST=self.host)

    def get(self, url, **kwargs):
        return self.client.get(url, secure=True, **kwargs)

    def post(self, url, data=None, **kwargs):
        return self.client.post(url, data=data or {}, secure=True, **kwargs)

    def create_user(self, *, username="testuser", password="StrongPass123!", phone=""):
        user = User.objects.create_user(username=username, password=password)
        user.userprofile.phone = phone
        user.userprofile.save(update_fields=["phone"])
        return user


class LoginViewTests(AuthViewTestCase):
    def setUp(self):
        super().setUp()
        self.login_url = reverse("login")

    def test_login_page_loads_with_current_form_contract(self):
        response = self.get(f"{self.login_url}?next=/profile/")

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context["form"], LoginForm)
        self.assertEqual(list(response.context["form"].fields), ["username", "password"])
        self.assertEqual(response.context["next"], "/profile/")

    def test_login_with_valid_credentials_redirects_home_for_completed_profile(self):
        user = self.create_user(phone="+380991234567")

        response = self.post(
            self.login_url,
            {"username": user.username, "password": "StrongPass123!"},
        )

        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.pk))

    def test_login_with_valid_credentials_redirects_to_profile_setup_when_phone_missing(self):
        user = self.create_user(username="empty-phone")

        response = self.post(
            self.login_url,
            {"username": user.username, "password": "StrongPass123!"},
        )

        self.assertRedirects(response, reverse("profile_setup"), fetch_redirect_response=False)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.pk))

    def test_login_with_invalid_credentials_returns_non_field_error(self):
        user = self.create_user(phone="+380991234567")

        response = self.post(
            self.login_url,
            {"username": user.username, "password": "wrongpassword"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].non_field_errors(),
            ["Невірний логін або пароль"],
        )
        self.assertIsNone(self.client.session.get("_auth_user_id"))

    def test_authenticated_user_is_redirected_from_login_to_profile_setup(self):
        user = self.create_user(phone="+380991234567")
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

        response = self.get(self.login_url)

        self.assertRedirects(response, reverse("profile_setup"), fetch_redirect_response=False)

    def test_login_honors_next_parameter_after_profile_check(self):
        user = self.create_user(phone="+380991234567")

        response = self.post(
            f"{self.login_url}?next=/profile/",
            {
                "username": user.username,
                "password": "StrongPass123!",
                "next": "/profile/",
            },
        )

        self.assertRedirects(response, "/profile/", fetch_redirect_response=False)


class RegisterViewTests(AuthViewTestCase):
    def setUp(self):
        super().setUp()
        self.register_url = reverse("register")

    def test_register_page_loads_with_current_form_contract(self):
        response = self.get(self.register_url)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context["form"], RegisterForm)
        self.assertEqual(
            list(response.context["form"].fields),
            ["username", "password1", "password2"],
        )

    def test_register_valid_data_creates_user_and_redirects_to_profile_setup(self):
        response = self.post(
            self.register_url,
            {
                "username": "newuser",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertRedirects(response, reverse("profile_setup"), fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username="newuser").exists())
        user = User.objects.get(username="newuser")
        self.assertEqual(self.client.session.get("_auth_user_id"), str(user.pk))

        self.assertEqual(user.userprofile.phone, "")

    def test_register_with_existing_username_returns_field_error(self):
        self.create_user(username="existinguser")

        response = self.post(
            self.register_url,
            {
                "username": "existinguser",
                "password1": "StrongPass123!",
                "password2": "StrongPass123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["username"],
            ["Користувач з таким логіном вже існує"],
        )

    def test_register_with_mismatched_passwords_returns_password2_error(self):
        response = self.post(
            self.register_url,
            {
                "username": "newuser",
                "password1": "StrongPass123!",
                "password2": "Mismatch123!",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["form"].errors["password2"],
            ["Паролі не співпадають"],
        )
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_register_with_weak_password_returns_password1_errors(self):
        response = self.post(
            self.register_url,
            {"username": "newuser", "password1": "123", "password2": "123"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("password1", response.context["form"].errors)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    def test_authenticated_user_is_redirected_from_register_to_profile_setup(self):
        user = self.create_user(phone="+380991234567")
        self.client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")

        response = self.get(self.register_url)

        self.assertRedirects(response, reverse("profile_setup"), fetch_redirect_response=False)


class LogoutViewTests(AuthViewTestCase):
    def setUp(self):
        super().setUp()
        self.logout_url = reverse("logout")
        self.user = self.create_user(phone="+380991234567")
        self.client.force_login(self.user, backend="django.contrib.auth.backends.ModelBackend")

    def test_logout_authenticated_user_redirects_home_and_clears_session(self):
        session = self.client.session
        session["cart"] = {"test": "data"}
        session.save()

        response = self.get(self.logout_url)

        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertIsNone(self.client.session.get("_auth_user_id"))
        self.assertIsNone(self.client.session.get("cart"))

    def test_logout_unauthenticated_user_redirects_home(self):
        self.client.logout()

        response = self.get(self.logout_url)

        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertIsNone(self.client.session.get("_auth_user_id"))


class ProfileSetupViewTests(AuthViewTestCase):
    def setUp(self):
        super().setUp()
        self.profile_setup_url = reverse("profile_setup")
        self.user = self.create_user(phone="+380991234567")
        self.client.force_login(self.user, backend="django.contrib.auth.backends.ModelBackend")

    def _delivery_payload(self):
        city_label = "м. Київ, Київ"
        city_ref = "delivery-city-ref"
        settlement_ref = "settlement-ref"
        warehouse_label = "Відділення №22, Київ, вул. Тестова, 1"
        return {
            "city": "довільний текст, який має бути проігнорований",
            "np_office": "ще один довільний текст",
            "np_settlement_ref": "spoofed-settlement-ref",
            "np_city_ref": "spoofed-city-ref",
            "np_city_token": build_city_choice_token(
                {
                    "label": city_label,
                    "settlement_ref": settlement_ref,
                    "city_ref": city_ref,
                }
            ),
            "np_warehouse_ref": "spoofed-warehouse-ref",
            "np_warehouse_token": build_warehouse_choice_token(
                {
                    "label": warehouse_label,
                    "ref": "warehouse-ref",
                    "kind": "branch",
                    "city_ref": city_ref,
                }
            ),
            "canonical_city": city_label,
            "canonical_np_office": warehouse_label,
        }

    def test_profile_setup_renders_profile_and_push_preferences_section(self):
        response = self.get(self.profile_setup_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["profile"], self.user.userprofile)
        self.assertContains(response, "Push-сповіщення")
        self.assertContains(response, 'data-web-push-profile')
        self.assertContains(response, "екран Додому")
        self.assertContains(response, "без App Store")

    def test_profile_setup_post_redirects_back_to_profile_setup(self):
        delivery = self._delivery_payload()
        response = self.post(
            self.profile_setup_url,
            {
                "full_name": "Test User",
                "phone": "+380991234567",
                "email": "user@example.com",
                "telegram": "@testuser",
                "instagram": "@twocomms",
                "city": delivery["city"],
                "np_office": delivery["np_office"],
                "np_settlement_ref": delivery["np_settlement_ref"],
                "np_city_ref": delivery["np_city_ref"],
                "np_city_token": delivery["np_city_token"],
                "np_warehouse_ref": delivery["np_warehouse_ref"],
                "np_warehouse_token": delivery["np_warehouse_token"],
                "pay_type": "full",
            },
        )

        self.assertRedirects(response, self.profile_setup_url, fetch_redirect_response=False)
        self.user.userprofile.refresh_from_db()
        self.assertEqual(self.user.userprofile.full_name, "Test User")
        self.assertEqual(self.user.userprofile.city, delivery["canonical_city"])
        self.assertEqual(self.user.userprofile.np_office, delivery["canonical_np_office"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "user@example.com")

    def test_profile_setup_rejects_non_image_upload(self):
        """W1-10 (NEW-502): не-изображение отклоняется валидацией формы."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        delivery = self._delivery_payload()
        fake_file = SimpleUploadedFile(
            "malware.php", b"<?php echo 'not an image'; ?>", content_type="image/png"
        )
        response = self.post(
            self.profile_setup_url,
            {
                "full_name": "Test User",
                "phone": "+380991234567",
                "np_city_token": delivery["np_city_token"],
                "np_warehouse_token": delivery["np_warehouse_token"],
                "pay_type": "full",
                "avatar": fake_file,
            },
        )

        # Невалидная форма — рендер страницы с ошибками, аватар не сохранён
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors.get("avatar"))
        self.user.userprofile.refresh_from_db()
        self.assertFalse(self.user.userprofile.avatar)

    def test_profile_setup_rejects_oversized_image(self):
        """W1-10: изображение больше 10 МБ отклоняется."""
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10)).save(buf, format="PNG")
        upload = SimpleUploadedFile("big.png", buf.getvalue(), content_type="image/png")
        upload.size = 11 * 1024 * 1024  # эмулируем 11 MB

        delivery = self._delivery_payload()
        response = self.post(
            self.profile_setup_url,
            {
                "full_name": "Test User",
                "phone": "+380991234567",
                "np_city_token": delivery["np_city_token"],
                "np_warehouse_token": delivery["np_warehouse_token"],
                "pay_type": "full",
                "avatar": upload,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors.get("avatar"))
        self.user.userprofile.refresh_from_db()
        self.assertFalse(self.user.userprofile.avatar)

    def test_profile_setup_push_preferences_form_updates_flags_independently(self):
        response = self.post(
            self.profile_setup_url,
            {
                "form_type": "push_preferences",
                "push_order_updates_enabled": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], f"{self.profile_setup_url}#push-preferences")
        self.user.userprofile.refresh_from_db()
        self.assertFalse(self.user.userprofile.push_marketing_enabled)
        self.assertTrue(self.user.userprofile.push_order_updates_enabled)

    def test_profile_setup_keeps_existing_ubd_doc_without_reupload(self):
        delivery = self._delivery_payload()
        self.user.userprofile.is_ubd = True
        self.user.userprofile.ubd_doc = SimpleUploadedFile(
            "ubd.jpg",
            b"existing-doc",
            content_type="image/jpeg",
        )
        self.user.userprofile.save(update_fields=["is_ubd", "ubd_doc"])

        response = self.post(
            self.profile_setup_url,
            {
                "full_name": "UBD User",
                "phone": "+380991234567",
                "email": "ubd@example.com",
                "city": delivery["city"],
                "np_office": delivery["np_office"],
                "np_settlement_ref": delivery["np_settlement_ref"],
                "np_city_ref": delivery["np_city_ref"],
                "np_city_token": delivery["np_city_token"],
                "np_warehouse_ref": delivery["np_warehouse_ref"],
                "np_warehouse_token": delivery["np_warehouse_token"],
                "pay_type": "full",
                "is_ubd": "on",
            },
        )

        self.assertRedirects(response, self.profile_setup_url, fetch_redirect_response=False)
        self.user.userprofile.refresh_from_db()
        self.assertTrue(self.user.userprofile.is_ubd)
        self.assertTrue(bool(self.user.userprofile.ubd_doc))
