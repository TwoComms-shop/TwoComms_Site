from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings

from twocomms.middleware import (
    SocialAuthStateCookieMiddleware,
    build_social_auth_state_cookie,
)


@override_settings(
    SOCIAL_AUTH_LOGIN_ERROR_URL="/login/",
    SOCIAL_AUTH_RAISE_EXCEPTIONS=False,
    SESSION_COOKIE_DOMAIN=".twocomms.shop",
    SESSION_COOKIE_SECURE=False,
)
class SocialAuthStateCookieMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = SocialAuthStateCookieMiddleware(lambda request: HttpResponse("ok"))

    def test_complete_restores_missing_google_state_from_signed_cookie(self):
        request = self.factory.get(
            "/oauth/complete/google-oauth2/",
            {"state": "oauth-state", "code": "fake-code"},
            secure=True,
            HTTP_HOST="twocomms.shop",
        )
        request.session = SessionStore()
        request.COOKIES["twc_oauth_state_google_oauth2"] = build_social_auth_state_cookie(
            "google-oauth2",
            "oauth-state",
        )

        self.middleware.process_request(request)

        self.assertEqual(request.session["google-oauth2_state"], "oauth-state")

    def test_login_response_sets_signed_state_cookie(self):
        request = self.factory.get(
            "/oauth/login/google-oauth2/",
            secure=True,
            HTTP_HOST="twocomms.shop",
        )
        request.session = SessionStore()
        request.session["google-oauth2_state"] = "oauth-state"
        response = HttpResponse(status=302)

        response = self.middleware.process_response(request, response)

        cookie = response.cookies["twc_oauth_state_google_oauth2"]
        self.assertTrue(cookie.value)
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["httponly"], True)

    def test_complete_ignores_mismatched_signed_cookie_state(self):
        request = self.factory.get(
            "/oauth/complete/google-oauth2/",
            {"state": "request-state", "code": "fake-code"},
            secure=True,
            HTTP_HOST="twocomms.shop",
        )
        request.session = SessionStore()
        request.COOKIES["twc_oauth_state_google_oauth2"] = build_social_auth_state_cookie(
            "google-oauth2",
            "different-state",
        )

        self.middleware.process_request(request)

        self.assertNotIn("google-oauth2_state", request.session)

    def test_primary_oauth_entry_clears_legacy_host_only_auth_cookies(self):
        request = self.factory.get(
            "/oauth/login/google-oauth2/",
            secure=True,
            HTTP_HOST="twocomms.shop",
        )
        request.session = SessionStore()
        response = HttpResponse(status=302)

        response = self.middleware.process_response(request, response)

        self.assertEqual(response.cookies["sessionid"]["domain"], "")
        self.assertEqual(response.cookies["sessionid"]["max-age"], 0)
        self.assertEqual(
            response.cookies["twc_oauth_state_google_oauth2"]["domain"], ""
        )

    def test_subdomain_oauth_entry_does_not_clear_shared_session_cookie(self):
        request = self.factory.get(
            "/oauth/login/google-oauth2/",
            secure=True,
            HTTP_HOST="management.twocomms.shop",
        )
        request.session = SessionStore()
        response = HttpResponse(status=302)

        response = self.middleware.process_response(request, response)

        self.assertNotIn("sessionid", response.cookies)


@override_settings(
    ALLOWED_HOSTS=["twocomms.shop", "testserver"],
    SOCIAL_AUTH_LOGIN_ERROR_URL="/login/",
    SOCIAL_AUTH_RAISE_EXCEPTIONS=False,
)
class SocialAuthExceptionMiddlewareTests(TestCase):
    def test_google_callback_without_session_state_redirects_to_login_instead_of_500(self):
        client = Client(HTTP_HOST="twocomms.shop", raise_request_exception=False)

        response = client.get(
            "/oauth/complete/google-oauth2/",
            {"state": "missing-session-state", "code": "fake-code"},
            secure=True,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/login/")
