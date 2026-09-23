import importlib
from types import SimpleNamespace
from unittest import TestCase


_migration = importlib.import_module(
    "management.migrations.0216_backfill_reviewed_no_reply_owner"
)


def _task(**overrides):
    values = {
        "skip_reason": "operator_reviewed_no_reply",
        "event_key": "ig-revision-debt:30",
        "event_payload": {"revision_id": 30},
        "manager_context": {
            "case_kind": "revision_execution_debt",
            "revision_id": 30,
            "operator_review": {
                "outcome": "reviewed_no_reply",
                "reply_confirmed": False,
                "actor_id": 2,
                "revision_id": 30,
            },
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LegacyReviewOwnerPredicateTests(TestCase):
    def test_backfills_only_complete_audited_shape(self):
        context = _migration._legacy_review_owner_context(_task())
        self.assertEqual(context["owner"], "manager")

    def test_rejects_reply_confirmed_or_untrusted_actor(self):
        for overrides in (
            {"manager_context": {**_task().manager_context, "owner": "bot"}},
            {"manager_context": {
                **_task().manager_context,
                "operator_review": {
                    **_task().manager_context["operator_review"],
                    "reply_confirmed": True,
                },
            }},
            {"manager_context": {
                **_task().manager_context,
                "operator_review": {
                    **_task().manager_context["operator_review"],
                    "actor_id": "2",
                },
            }},
        ):
            with self.subTest(overrides=overrides):
                self.assertIsNone(_migration._legacy_review_owner_context(_task(**overrides)))

    def test_rejects_identity_or_operator_disposition_drift(self):
        for overrides in (
            {"skip_reason": "other"},
            {"event_payload": {"revision_id": 31}},
            {"event_key": "ig-revision-debt:31"},
            {"manager_context": {**_task().manager_context, "revision_id": 31}},
        ):
            with self.subTest(overrides=overrides):
                self.assertIsNone(_migration._legacy_review_owner_context(_task(**overrides)))
