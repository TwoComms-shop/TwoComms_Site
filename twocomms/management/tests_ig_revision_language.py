from copy import deepcopy

from django.test import TransactionTestCase
from django.utils import timezone

from management import tests_ig_revision_live as fixtures
from management.models import IgCustomerTurn, IgTurnMessage
from management.services.ig_revision_commerce import reduce_revision_commerce
from management.services.ig_revision_outbox import PublicationBinding
from management.services.ig_turn_revisions import create_collecting_revision


class RevisionLanguageTests(TransactionTestCase):
    setUp = fixtures.RevisionLiveTests.setUp
    _message = fixtures.RevisionLiveTests._message
    _prepare = fixtures.RevisionLiveTests._prepare

    def test_explicit_language_is_persisted_once_with_original_source(self):
        source = self._message("Please reply in English", "language-request")
        turn = IgCustomerTurn.objects.create(
            client=self.customer, primary_source_message=source,
            window_started_at=timezone.now(), window_deadline=timezone.now(),
        )
        IgTurnMessage.objects.create(turn=turn, message=source, ordinal=1, role="user")
        self.revision = create_collecting_revision(turn, [source], bypass_quiet=True).revision
        self._prepare()
        arguments = dict(
            settings_id=self.settings.pk, settings_permission_epoch=self.settings.reply_permission_epoch,
            publication=PublicationBinding(self.publication.pk, self.publication.version, self.publication.snapshot_hash),
        )
        first = reduce_revision_commerce(self.revision.pk, self.token, **arguments)
        self.assertTrue(first.ready, first.reason)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.language, "en")
        recorded_context = deepcopy(self.customer.sales_context)
        second = reduce_revision_commerce(self.revision.pk, self.token, **arguments)
        self.assertTrue(second.replayed, second.reason)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.sales_context, recorded_context)
