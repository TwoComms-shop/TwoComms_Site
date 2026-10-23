"""Publish reviewed owner scenarios and localized trade policy using existing CAS."""
from hashlib import sha256

from django.db import transaction

from management.knowledge.scenarios import SCENARIO_INSTRUCTIONS, VERSION, reviewed_scenario_values
from management.knowledge.publication_compat import (
    LEGACY_COMPATIBILITY_TITLES, legacy_instruction_compatibility_plan,
    reviewed_legacy_instruction_values,
)
from management.services.ig_policy_publication import (
    PolicyPublicationError, draft_state, load_active_policy_snapshot,
    preview_instruction_draft, publish_instruction_policy, save_instruction_draft,
)

_CASES = (
    ("custom_print", {"custom_print"}, {"custom_print"}),
    ("collaboration", {"collaboration"}, {"collaboration"}),
    ("ugc", {"ugc"}, {"ugc"}),
    ("size", {"size"}, {"size_question"}),
    ("objection", {"objection_price"}, {"price_objection"}),
)


def _matching(item, values):
    scalar = ("title", "body", "active", "priority", "locale", "trust_scope")
    return (all(item.get(key) == values[key] for key in scalar)
        and all(sorted(item.get(key) or ()) == sorted(values[key]) for key in ("tags", "triggers", "allowed_actions"))
        and item.get("programme_metadata", {}) == values["programme_metadata"]
        and item.get("reviewed_source", {}) == values.get("reviewed_source", {}))


def _plan():
    state, head = draft_state(), load_active_policy_snapshot()
    titles = {row["title"] for row in SCENARIO_INSTRUCTIONS}
    def unmanaged(snapshot):
        return [item for item in snapshot["instructions"] if item["title"] not in titles]
    if unmanaged(state.snapshot) != unmanaged(head.snapshot):
        raise PolicyPublicationError("unrelated_instruction_draft_changes",
            "Another instruction draft must be reviewed separately before publishing owner scenarios")
    for item in state.snapshot["instructions"]:
        if item["title"] in LEGACY_COMPATIBILITY_TITLES and reviewed_legacy_instruction_values(item) is None:
            raise PolicyPublicationError("legacy_instruction_requires_review",
                "A targeted instruction differs from its reviewed source")
    compatibility = legacy_instruction_compatibility_plan(state.snapshot["instructions"])
    changes = []
    for row in SCENARIO_INSTRUCTIONS:
        values = reviewed_scenario_values(row["key"])
        matches = [item for item in state.snapshot["instructions"] if item["title"] == values["title"]]
        if len(matches) > 1:
            raise PolicyPublicationError("owner_scenario_title_ambiguous", "Duplicate reviewed scenario title")
        item = matches[0] if matches else None
        changes.append({"key": row["key"], "instruction_id": item["source_id"] if item else None,
            "action": "create" if item is None else "unchanged" if _matching(item, values) else "update",
            "body_digest": sha256(values["body"].encode()).hexdigest()})
    return state, head, changes, compatibility


def publish_reviewed_owner_scenarios(*, apply=False, expected_draft_hash="", expected_head_hash=""):
    """The default is read-only. Unrelated draft edits are never published."""
    if apply:
        if not expected_draft_hash or not expected_head_hash:
            raise PolicyPublicationError("owner_publication_cas_required", "Draft and head hashes are required")
        with transaction.atomic():
            from management.models import InstagramBotSettings
            InstagramBotSettings.objects.select_for_update().get(pk=1)
            return _publish(expected_draft_hash, expected_head_hash)
    state, head, changes, compatibility = _plan()
    return {"mode": "dry-run", "version": VERSION, "draft_hash": state.snapshot_hash,
        "draft_revision": state.revision, "head_id": head.publication_id, "head_hash": head.snapshot_hash,
        "changes": changes, "compatibility": [{key: row[key] for key in
            ("instruction_id", "before_body_digest", "after_body_digest")} for row in compatibility],
        "provider_io": False}


def _publish(expected_draft_hash, expected_head_hash):
    state, head, changes, compatibility = _plan()
    if state.snapshot_hash != expected_draft_hash or head.snapshot_hash != expected_head_hash:
        raise PolicyPublicationError("owner_publication_cas_changed", "Reviewed draft or publication changed")
    for change in compatibility:
        _instruction, state = save_instruction_draft(
            expected_revision=state.revision, expected_snapshot_hash=state.snapshot_hash,
            values=change["values"], instruction_id=change["instruction_id"],
            note="Owner-reviewed compatibility: source-hashed body only; retain routing and controls")
    for change in changes:
        if change["action"] == "unchanged":
            continue
        _instruction, state = save_instruction_draft(
            expected_revision=state.revision, expected_snapshot_hash=state.snapshot_hash,
            values=reviewed_scenario_values(change["key"]), instruction_id=change["instruction_id"],
            note=f"{VERSION}: owner answers reviewed; preserve unrelated instructions")
    coverage = {}
    for scenario, tags, triggers in _CASES:
        preview = preview_instruction_draft(expected_revision=state.revision,
            expected_snapshot_hash=state.snapshot_hash, client_tags=tags, active_triggers=triggers)
        admission = preview["required_policy"]
        if scenario not in admission["covered_scenarios"] or admission["coverage_status"] != "declared_covered":
            raise PolicyPublicationError("owner_scenario_coverage_missing", "Reviewed scenario is not admitted")
        coverage[scenario] = {"status": admission["coverage_status"], "required_ids": preview["required_ids"]}
    # Localized trade facts are required only on the existing owned business
    # facet. Creator/Direct and ordinary retail previews must not receive them.
    from management.services.ig_cooperation_policy import LANGUAGES, TITLE
    for locale in LANGUAGES:
        preview = preview_instruction_draft(expected_revision=state.revision,
            expected_snapshot_hash=state.snapshot_hash, locale=locale, client_tags={"collaboration", locale},
            active_triggers={"collaboration", "business_cooperation"}, budget_chars=48000)
        selected = preview.get("selected") or []
        trade = [item for item in selected if item.get("title") == f"{TITLE} ({locale})"]
        if len(trade) != 1 or trade[0]["id"] not in preview["required_ids"]:
            raise PolicyPublicationError("cooperation_policy_coverage_missing", "Reviewed business module is not admitted")
        coverage["business_cooperation_" + locale] = {"required_id": trade[0]["id"]}
    for triggers, tags in (({"collaboration"}, {"collaboration"}), (set(), {"product"})):
        preview = preview_instruction_draft(expected_revision=state.revision,
            expected_snapshot_hash=state.snapshot_hash, client_tags=tags, active_triggers=triggers)
        if any(str(item.get("title") or "").startswith(TITLE) for item in preview.get("selected") or []):
            raise PolicyPublicationError("cooperation_policy_relevance_invalid", "Trade policy entered another route")
    result = publish_instruction_policy(expected_draft_revision=state.revision,
        expected_draft_hash=state.snapshot_hash, expected_head_id=head.publication_id,
        expected_head_hash=head.snapshot_hash, note=f"{VERSION}: reviewed scenarios and localized business policy")
    return {"mode": "applied", "version": VERSION, "changed": result.changed,
        "previous_head_id": head.publication_id, "previous_head_hash": head.snapshot_hash,
        "head_id": result.publication.pk, "head_hash": result.publication.snapshot_hash,
        "coverage": coverage, "compatibility_updated_ids": [row["instruction_id"] for row in compatibility],
        "provider_io": False}
