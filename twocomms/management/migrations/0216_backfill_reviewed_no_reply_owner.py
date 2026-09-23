from django.db import migrations


def _legacy_review_owner_context(task):
    context = task.manager_context
    if not isinstance(context, dict) or context.get("owner") not in (None, ""):
        return None
    review = context.get("operator_review")
    revision_id = context.get("revision_id")
    payload = task.event_payload
    if (
        task.skip_reason != "operator_reviewed_no_reply"
        or not isinstance(review, dict)
        or review.get("outcome") != "reviewed_no_reply"
        or review.get("reply_confirmed") is not False
        or not isinstance(review.get("actor_id"), int)
        or review.get("actor_id") <= 0
        or not isinstance(revision_id, int)
        or revision_id <= 0
        or review.get("revision_id") != revision_id
        or not isinstance(payload, dict)
        or payload.get("revision_id") != revision_id
        or task.event_key != f"ig-revision-debt:{revision_id}"
    ):
        return None
    return {**context, "owner": "manager"}


def backfill_legacy_review_owners(apps, schema_editor):
    """Normalize pre-owner review closures without changing delivery truth.

    Older operator-review rows predate the explicit ``owner`` field in the
    manager context. They were already cancelled with an auditable
    ``reviewed_no_reply`` disposition, but the health classifier intentionally
    requires an accountable owner before considering the case closed. Add that
    missing context key only when the event and revision identity match.
    """
    del schema_editor
    task_model = apps.get_model("management", "IgFollowUpTask")
    queryset = task_model.objects.filter(
        kind="manager_task",
        reason="revision_case:execution_debt",
        status="cancelled",
    )
    for task in queryset.iterator():
        context = _legacy_review_owner_context(task)
        if context is None:
            continue
        task.manager_context = context
        task.save(update_fields=["manager_context", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("management", "0215_gemini_38_quota_profile")]

    operations = [
        migrations.RunPython(backfill_legacy_review_owners, migrations.RunPython.noop),
    ]
