"""Captured revision references stay private, bounded, and read-only."""
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path, reverse
from django.utils import timezone
from management import tests_ig_request_preview_api as preview_fixtures
from management.bot_access import OPERATE_IG_BOT_PERMISSION, VIEW_IG_CONVERSATION_PII_PERMISSION
from management.bot_request_preview_views import bot_request_revision_index_api
from management.models import GeminiRequest, IgClient

urlpatterns = [*preview_fixtures.urlpatterns, path(
    'bot/api/clients/<int:client_id>/request-revisions/', bot_request_revision_index_api,
    name='management_bot_request_revision_index_api')]


@override_settings(ROOT_URLCONF='management.tests_ig_request_revision_index', SECURE_SSL_REDIRECT=False,
                   GOOGLE_INDEXING_ENABLED=False, IG_REVISION_EXECUTION_ENABLED=False)
class RequestRevisionIndexTests(TransactionTestCase):
    setUp = preview_fixtures.RequestPreviewApiTests.setUp
    grant = staticmethod(preview_fixtures.RequestPreviewApiTests.grant)

    def url(self, identity=None):
        return reverse('management_bot_request_revision_index_api', args=[
            self.case.customer.pk if identity is None else identity])

    def test_each_capability_is_required_before_accounting_read(self):
        for index, permissions in enumerate(((), (OPERATE_IG_BOT_PERMISSION,), (VIEW_IG_CONVERSATION_PII_PERMISSION,))):
            actor = get_user_model().objects.create_user(username=f'index-private-{index}', is_staff=True)
            self.grant(actor, *permissions)
            self.client.force_login(actor)
            with self.subTest(permissions=permissions), patch.object(GeminiRequest.objects, 'filter') as rows:
                self.assertEqual(self.client.get(self.url()).status_code, 403)
                rows.assert_not_called()

    def test_repeated_get_lists_only_owned_captured_refs_without_dml_or_payloads(self):
        foreign = IgClient.objects.create(igsid='request-index-foreign')
        with CaptureQueriesContext(connection) as queries:
            first = self.client.get(self.url())
            second = self.client.get(self.url())
            isolated = self.client.get(self.url(foreign.pk))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual([item['revision_id'] for item in first.json()['revisions']], [self.case.revision.pk])
        self.assertEqual(isolated.json()['revisions'], [])
        self.assertNotIn('request_context', first.content.decode())
        self.assertNotIn('synthetic private prompt', first.content.decode())
        self.assertIn('no-store', first.headers['Cache-Control'])
        self.assertTrue(first.json()['coverage_complete'])
        self.assertLessEqual(len(first.json()['revisions']), first.json()['limit'])
        for row in queries:
            self.assertIn(row['sql'].lstrip().split()[0].upper(), {'SELECT', 'BEGIN', 'SAVEPOINT', 'RELEASE', 'COMMIT', 'ROLLBACK'})
        accounting_reads = [row['sql'] for row in queries if 'management_geminirequest' in row['sql'].lower()]
        self.assertEqual(len(accounting_reads), 3)
        self.assertTrue(all('LIMIT 11' in sql.upper() for sql in accounting_reads))

    def test_erasure_and_out_of_range_identity_do_not_expose_revisions(self):
        with patch.object(GeminiRequest.objects, 'filter') as rows:
            self.assertEqual(self.client.get(self.url(2**63)).status_code, 400)
            rows.assert_not_called()
        IgClient.objects.filter(pk=self.case.customer.pk).update(privacy_erasure_started_at=timezone.now())
        with patch.object(GeminiRequest.objects, 'filter') as rows:
            response = self.client.get(self.url())
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()['revisions'], [])
            rows.assert_not_called()
