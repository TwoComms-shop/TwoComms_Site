"""Atomic draft and immutable publication primitives for IG instructions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from django.db import transaction

from management.services.bot_instruction_routing import (
    TURN_TRIGGER_NAMES,
    instruction_matches,
    split_instruction_tags,
    validate_instruction_tags,
)
from management.services.ig_response_control import PROVIDER_CONTROL_KINDS


SNAPSHOT_SCHEMA_VERSION = 1
PUBLICATION_COMPILER_VERSION = "instruction-set-v1"
MAX_INSTRUCTIONS = 300
MAX_BODY_CHARS = 64_000
MAX_TRIGGER_CODES = 32
MAX_ALLOWED_ACTIONS = 32
DEFAULT_INSTRUCTION_BUDGET_CHARS = 3500
RESERVED_PROGRAMME_TAG = "programme:shooting_prize"
PROGRAMME_ID = "shooting_prize"
LOCALES = frozenset({"all", "uk", "ru", "en"})
TRUST_SCOPES = frozenset({"public_policy", "operator_only"})
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TAG_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")

# These are proposal kinds with a current parser and downstream consumer. The
# editable declaration never grants authority: all existing business, payment,
# channel and truth gates still run after proposal parsing.
ACTUAL_PROPOSAL_CONSUMERS = frozenset({
    "manager", "spam", "stage", "paylink", "payment", "product", "item",
    "option", "qty", "size", "fit", "color_variant_id", "price_quoted",
    "order", "show_products", "catalog_link", "objhandle",
})
PROPOSAL_VISIBILITY_ACTIONS = frozenset(PROVIDER_CONTROL_KINDS) & ACTUAL_PROPOSAL_CONSUMERS
# Preserve the current immutable-core behavior when a selected text-only module
# declares no actions. A later narrowing needs an explicit compatible consumer.
BASELINE_PROPOSAL_ACTIONS = PROPOSAL_VISIBILITY_ACTIONS


class PolicyPublicationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DraftRevisionConflict(PolicyPublicationError):
    def __init__(self):
        super().__init__("draft_revision_conflict", "instruction draft changed")


class PublicationHeadConflict(PolicyPublicationError):
    def __init__(self):
        super().__init__("publication_head_conflict", "published instruction head changed")


@dataclass(frozen=True)
class DraftState:
    revision: int
    snapshot: dict
    snapshot_hash: str


@dataclass(frozen=True)
class PublicationResult:
    publication: object
    changed: bool


@dataclass(frozen=True)
class ActivePolicySnapshot:
    publication_id: int
    version: int
    snapshot_hash: str
    compiler_version: str
    snapshot: dict


def _actor_label(actor) -> str:
    if not actor:
        return ""
    return str(
        getattr(actor, "get_full_name", lambda: "")()
        or getattr(actor, "username", "")
    )[:150]


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_hash(snapshot: dict) -> str:
    return sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


def _bounded_string_list(
    values,
    *,
    code: str,
    maximum: int,
    pattern: re.Pattern = _ACTION_RE,
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise PolicyPublicationError(code, f"{code} must be a list")
    normalized = []
    for raw in values:
        value = str(raw or "").strip().casefold()
        if not value or not pattern.fullmatch(value):
            raise PolicyPublicationError(code, f"{code} contains an invalid value")
        if value not in normalized:
            normalized.append(value)
        if len(normalized) > maximum:
            raise PolicyPublicationError(code, f"{code} exceeds its bounded size")
    return tuple(sorted(normalized))


def _routing_fields(row) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = str(getattr(row, "intent_tags", "") or "")
    split = split_instruction_tags(raw)
    tags = set(split["plain"])
    tags.update(f"not:{value}" for value in split["excludes"])
    triggers = set(split["triggers"])
    explicit = getattr(row, "trigger_codes", []) or []
    triggers.update(_bounded_string_list(
        explicit,
        code="invalid_trigger_codes",
        maximum=MAX_TRIGGER_CODES,
    ))
    if not triggers.issubset(TURN_TRIGGER_NAMES):
        raise PolicyPublicationError(
            "unknown_trigger_codes", "instruction contains an unknown trigger"
        )
    reconstructed = ",".join([*sorted(tags), *(f"on:{value}" for value in sorted(triggers))])
    validation_tags = [tag for tag in tags if tag != RESERVED_PROGRAMME_TAG]
    validation_raw = ",".join([
        *sorted(validation_tags),
        *(f"on:{value}" for value in sorted(triggers)),
    ])
    issues = validate_instruction_tags(validation_raw)
    if issues["unknown_tags"]:
        raise PolicyPublicationError(
            "unknown_instruction_tags", "instruction contains an unknown audience tag"
        )
    return tuple(sorted(tags)), tuple(sorted(triggers))


def _programme_metadata(row, tags: tuple[str, ...]) -> dict:
    raw = getattr(row, "programme_metadata", {}) or {}
    if not isinstance(raw, dict):
        raise PolicyPublicationError(
            "invalid_programme_metadata", "programme metadata must be an object"
        )
    if not raw and RESERVED_PROGRAMME_TAG in tags:
        return {
            "kind": PROGRAMME_ID,
            "programme_id": PROGRAMME_ID,
            "manager_required": True,
            "confirmed_visual_sample": False,
        }
    if not raw:
        return {}
    expected = {
        "kind", "programme_id", "manager_required", "confirmed_visual_sample"
    }
    if (
        set(raw) != expected
        or RESERVED_PROGRAMME_TAG not in tags
        or raw.get("kind") != PROGRAMME_ID
        or raw.get("programme_id") != PROGRAMME_ID
        or raw.get("manager_required") is not True
        or raw.get("confirmed_visual_sample") is not False
    ):
        raise PolicyPublicationError(
            "invalid_programme_metadata",
            "only the reserved shooting programme metadata is supported",
        )
    return {
        "kind": PROGRAMME_ID,
        "programme_id": PROGRAMME_ID,
        "manager_required": True,
        "confirmed_visual_sample": False,
    }


def _instruction_item(row) -> dict:
    source_id = int(getattr(row, "pk", 0) or 0)
    if source_id <= 0:
        raise PolicyPublicationError("invalid_instruction_id", "instruction must be saved")
    title = str(getattr(row, "title", "") or "").strip()[:200]
    body = str(getattr(row, "body", "") or "").strip()
    if len(body) > MAX_BODY_CHARS:
        raise PolicyPublicationError("instruction_body_too_large", "instruction body is too large")
    locale = str(getattr(row, "locale", "all") or "all").strip().casefold()
    trust_scope = str(
        getattr(row, "trust_scope", "public_policy") or "public_policy"
    ).strip().casefold()
    if locale not in LOCALES:
        raise PolicyPublicationError("invalid_instruction_locale", "instruction locale is invalid")
    if trust_scope not in TRUST_SCOPES:
        raise PolicyPublicationError("invalid_trust_scope", "instruction trust scope is invalid")
    tags, triggers = _routing_fields(row)
    actions = _bounded_string_list(
        getattr(row, "allowed_actions", []) or [],
        code="invalid_allowed_actions",
        maximum=MAX_ALLOWED_ACTIONS,
    )
    if not set(actions).issubset(PROPOSAL_VISIBILITY_ACTIONS):
        raise PolicyPublicationError(
            "unsupported_allowed_action",
            "allowed action has no current hard server consumer",
        )
    programme = _programme_metadata(row, tags)
    item = {
        "id": f"instruction:{source_id}",
        "source_id": source_id,
        "title": title,
        "body": body,
        "active": bool(getattr(row, "is_active", False)),
        "priority": int(getattr(row, "priority", 100)),
        "locale": locale,
        "tags": list(tags),
        "triggers": list(triggers),
        "programme_metadata": programme,
        "allowed_actions": list(actions),
        "trust_scope": trust_scope,
    }
    provenance = _reviewed_source(getattr(row, "reviewed_source", {}), body)
    if provenance:
        if trust_scope != "public_policy":
            raise PolicyPublicationError("brand_provenance_scope_invalid", "reviewed brand source must be public policy")
        item["reviewed_source"] = provenance
    return item


def _reviewed_source(value, body):
    from management.services.ig_brand_source import BrandSourceError, validate_reviewed_source

    try:
        return validate_reviewed_source(value, body)
    except BrandSourceError as exc:
        raise PolicyPublicationError(exc.code, "reviewed brand provenance is invalid") from exc


def snapshot_from_rows(rows) -> dict:
    items = [_instruction_item(row) for row in rows]
    if len(items) > MAX_INSTRUCTIONS:
        raise PolicyPublicationError(
            "instruction_count_exceeded", "instruction set exceeds its bounded size"
        )
    items.sort(key=lambda item: (item["priority"], item["source_id"]))
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "instructions": items,
    }


def _snapshot_items(snapshot) -> list[dict]:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or not isinstance(snapshot.get("instructions"), list)
        or len(snapshot["instructions"]) > MAX_INSTRUCTIONS
    ):
        raise PolicyPublicationError("invalid_policy_snapshot", "policy snapshot is invalid")
    items = list(snapshot["instructions"])
    for item in items:
        if not isinstance(item, dict):
            raise PolicyPublicationError("invalid_policy_snapshot", "instruction item is invalid")
        if "reviewed_source" in item:
            _reviewed_source(item["reviewed_source"], str(item.get("body") or ""))
            if item.get("trust_scope") != "public_policy":
                raise PolicyPublicationError("brand_provenance_scope_invalid", "reviewed brand source must be public policy")
    return items


def select_policy_snapshot(
    snapshot: dict,
    *,
    locale: str = "all",
    client_tags=None,
    active_triggers=(),
    budget_chars: int = DEFAULT_INSTRUCTION_BUDGET_CHARS,
    public_only: bool = True,
) -> dict:
    """Select whole modules with the same routing and byte budget as runtime."""
    requested_locale = str(locale or "all").strip().casefold()
    if requested_locale not in LOCALES:
        raise PolicyPublicationError("invalid_preview_locale", "preview locale is invalid")
    budget = int(budget_chars)
    if budget < 0:
        raise PolicyPublicationError("invalid_preview_budget", "preview budget is invalid")
    tags = None if client_tags is None else {str(value).casefold() for value in client_tags}
    triggers = {str(value).casefold() for value in active_triggers}
    selected = []
    omitted = []
    used = 0
    declared_actions = set()
    for item in _snapshot_items(snapshot):
        module_id = str(item.get("id") or "")
        if public_only and item.get("trust_scope") != "public_policy":
            omitted.append({"id": module_id, "reason": "operator_only"})
            continue
        if not item.get("active"):
            omitted.append({"id": module_id, "reason": "inactive"})
            continue
        body = str(item.get("body") or "").strip()
        if not body:
            omitted.append({"id": module_id, "reason": "empty_body"})
            continue
        item_locale = str(item.get("locale") or "all")
        if requested_locale != "all" and item_locale not in {"all", requested_locale}:
            omitted.append({"id": module_id, "reason": "locale_mismatch"})
            continue
        raw_tags = ",".join([
            *(str(value) for value in item.get("tags") or []),
            *(f"on:{value}" for value in item.get("triggers") or []),
        ])
        if tags is not None and not instruction_matches(
            raw_tags,
            tags,
            active_triggers=triggers,
        ):
            omitted.append({"id": module_id, "reason": "not_relevant"})
            continue
        title = str(item.get("title") or "").strip()
        rendered = f"• {title}: {body}" if title else f"• {body}"
        cost = len(rendered) + (1 if selected else 0)
        if used + cost > budget:
            omitted.append({"id": module_id, "reason": "budget_exhausted"})
            continue
        selected.append({**item, "rendered_body": rendered})
        used += cost
        declared_actions.update(item.get("allowed_actions") or [])
    effective_actions = (
        set(BASELINE_PROPOSAL_ACTIONS) | declared_actions
    ) & set(PROPOSAL_VISIBILITY_ACTIONS)
    return {
        "selected": selected,
        "selected_ids": [item["id"] for item in selected],
        "omitted": omitted,
        "rendered_text": "\n".join(item["rendered_body"] for item in selected),
        "used_chars": used,
        "declared_actions": sorted(declared_actions),
        "effective_proposal_actions": sorted(effective_actions),
        "snapshot_hash": snapshot_hash(snapshot),
        "compiler_version": PUBLICATION_COMPILER_VERSION,
    }


def _settings_lock():
    from management.models import InstagramBotSettings

    settings_obj, _created = InstagramBotSettings.objects.get_or_create(pk=1)
    return InstagramBotSettings.objects.select_for_update().get(pk=settings_obj.pk)


def _current_rows(*, lock: bool):
    from management.models import BotInstruction

    queryset = BotInstruction.objects.all().order_by("priority", "id")
    return list(queryset.select_for_update() if lock else queryset)


def draft_state() -> DraftState:
    from management.models import InstagramBotSettings

    settings_obj = InstagramBotSettings.objects.filter(pk=1).first()
    snapshot = snapshot_from_rows(_current_rows(lock=False))
    return DraftState(
        revision=int(getattr(settings_obj, "instruction_draft_revision", 0) or 0),
        snapshot=deepcopy(snapshot),
        snapshot_hash=snapshot_hash(snapshot),
    )


def load_active_policy_snapshot(settings_obj=None) -> ActivePolicySnapshot:
    """Read one immutable head; never fall back to mutable draft rows."""
    from management.models import BotPolicyPublication, InstagramBotSettings

    settings_obj = settings_obj or InstagramBotSettings.load()
    publication_id = getattr(settings_obj, "active_instruction_publication_id", None)
    if not publication_id:
        raise PolicyPublicationError(
            "active_publication_missing",
            "published instruction snapshot is not initialized",
        )
    publication = BotPolicyPublication.objects.filter(pk=publication_id).first()
    if publication is None:
        raise PolicyPublicationError(
            "active_publication_missing",
            "published instruction snapshot is unavailable",
        )
    snapshot = publication.snapshot
    _snapshot_items(snapshot)
    if snapshot_hash(snapshot) != publication.snapshot_hash:
        raise PolicyPublicationError(
            "active_publication_hash_mismatch",
            "published instruction snapshot failed integrity validation",
        )
    return ActivePolicySnapshot(
        publication_id=int(publication.pk),
        version=int(publication.version),
        snapshot_hash=str(publication.snapshot_hash),
        compiler_version=str(publication.compiler_version),
        snapshot=snapshot,
    )


@transaction.atomic
def preview_instruction_draft(
    *,
    expected_revision: int,
    expected_snapshot_hash: str,
    locale: str = "all",
    client_tags=None,
    active_triggers=(),
    budget_chars: int = DEFAULT_INSTRUCTION_BUDGET_CHARS,
) -> dict:
    """CAS one coherent draft and run the selector used by live consumption."""
    settings_obj = _settings_lock()
    if int(settings_obj.instruction_draft_revision or 0) != int(expected_revision):
        raise DraftRevisionConflict()
    snapshot = snapshot_from_rows(_current_rows(lock=True))
    digest = snapshot_hash(snapshot)
    if digest != str(expected_snapshot_hash or ""):
        raise DraftRevisionConflict()
    result = select_policy_snapshot(
        snapshot,
        locale=locale,
        client_tags=client_tags,
        active_triggers=active_triggers,
        budget_chars=budget_chars,
        public_only=True,
    )
    return {
        **result,
        "draft_revision": int(settings_obj.instruction_draft_revision),
        "instruction_count": len(snapshot["instructions"]),
    }


def publication_history(*, limit: int = 50) -> list[dict]:
    """Return content-free publication history for the editor UI."""
    from management.models import BotPolicyPublication

    bounded = max(1, min(int(limit), 200))
    return [
        {
            "id": row.pk,
            "version": int(row.version),
            "kind": row.kind,
            "parent_id": row.parent_id,
            "restored_from_id": row.restored_from_id,
            "snapshot_hash": row.snapshot_hash,
            "compiler_version": row.compiler_version,
            "instruction_count": int(row.instruction_count),
            "actor": row.actor_label or "автоматизація",
            "note": row.note,
            "created_at": row.created_at.isoformat(),
        }
        for row in BotPolicyPublication.objects.only(
            "id", "version", "kind", "parent_id", "restored_from_id",
            "snapshot_hash", "compiler_version", "instruction_count",
            "actor_label", "note", "created_at",
        ).order_by("-version")[:bounded]
    ]


def _audit(*, actor, action: str, entity_id: str, before: dict, after: dict, note: str):
    from management.models import AdminAuditLog

    return AdminAuditLog.objects.create(
        actor=actor if getattr(actor, "pk", None) else None,
        actor_role="prompt_editor",
        action=action,
        entity_type="bot_instruction_policy",
        entity_id=str(entity_id or "")[:64],
        before=before,
        after=after,
        reason=str(note or "")[:500],
    )


def _normalize_draft_values(values: dict) -> dict:
    raw = dict(values or {})
    locale = str(raw.get("locale") or "all").strip().casefold()
    trust_scope = str(raw.get("trust_scope") or "public_policy").strip().casefold()
    if locale not in LOCALES or trust_scope not in TRUST_SCOPES:
        raise PolicyPublicationError("invalid_instruction_metadata", "instruction metadata is invalid")
    tags = _bounded_string_list(
        raw.get("tags") or [],
        code="invalid_instruction_tags",
        maximum=64,
        pattern=_TAG_RE,
    )
    triggers = _bounded_string_list(
        raw.get("triggers") or [], code="invalid_trigger_codes", maximum=MAX_TRIGGER_CODES
    )
    if not set(triggers).issubset(TURN_TRIGGER_NAMES):
        raise PolicyPublicationError("unknown_trigger_codes", "instruction contains an unknown trigger")
    actions = _bounded_string_list(
        raw.get("allowed_actions") or [],
        code="invalid_allowed_actions",
        maximum=MAX_ALLOWED_ACTIONS,
    )
    if not set(actions).issubset(PROPOSAL_VISIBILITY_ACTIONS):
        raise PolicyPublicationError(
            "unsupported_allowed_action", "allowed action has no current consumer"
        )
    tag_text = ",".join([*tags, *(f"on:{value}" for value in triggers)])
    if len(tag_text) > 400:
        raise PolicyPublicationError(
            "instruction_tags_too_large", "instruction routing metadata is too large"
        )
    validation_tag_text = ",".join(
        tag for tag in tag_text.split(",") if tag != RESERVED_PROGRAMME_TAG
    )
    issues = validate_instruction_tags(validation_tag_text)
    if issues["unknown_tags"]:
        raise PolicyPublicationError("unknown_instruction_tags", "instruction tag is unknown")
    programme = raw.get("programme_metadata") or {}
    probe = type("InstructionProbe", (), {
        "programme_metadata": programme,
    })()
    programme = _programme_metadata(probe, tags)
    body = str(raw.get("body") or "")
    title = str(raw.get("title") or "")
    if len(title) > 200:
        raise PolicyPublicationError("instruction_title_too_large", "instruction title is too large")
    if len(body) > MAX_BODY_CHARS:
        raise PolicyPublicationError("instruction_body_too_large", "instruction body is too large")
    active = raw.get("active", True)
    priority = raw.get("priority", 100)
    if not isinstance(active, bool):
        raise PolicyPublicationError("invalid_instruction_active", "active must be boolean")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise PolicyPublicationError("invalid_instruction_priority", "priority must be integer")
    if not -10_000 <= priority <= 10_000:
        raise PolicyPublicationError("invalid_instruction_priority", "priority is outside bounds")
    provenance = _reviewed_source(raw.get("reviewed_source", {}), body)
    if provenance and trust_scope != "public_policy":
        raise PolicyPublicationError("brand_provenance_scope_invalid", "reviewed brand source must be public policy")
    return {
        "title": title,
        "body": body,
        "intent_tags": tag_text,
        "is_active": active,
        "priority": priority,
        "locale": locale,
        "trigger_codes": list(triggers),
        "programme_metadata": programme,
        "allowed_actions": list(actions),
        "trust_scope": trust_scope,
        "reviewed_source": provenance,
    }


@transaction.atomic
def save_instruction_draft(
    *,
    expected_revision: int,
    expected_snapshot_hash: str,
    values: dict,
    instruction_id: int | None = None,
    actor=None,
    note: str = "",
) -> tuple[object, DraftState]:
    from management.models import BotInstruction

    settings_obj = _settings_lock()
    if int(settings_obj.instruction_draft_revision or 0) != int(expected_revision):
        raise DraftRevisionConflict()
    before_snapshot = snapshot_from_rows(_current_rows(lock=True))
    if snapshot_hash(before_snapshot) != str(expected_snapshot_hash or ""):
        raise DraftRevisionConflict()
    normalized = _normalize_draft_values(values)
    if instruction_id is None:
        instruction = BotInstruction()
    else:
        instruction = BotInstruction.objects.select_for_update().filter(
            pk=instruction_id
        ).first()
        if instruction is None:
            raise PolicyPublicationError("instruction_not_found", "instruction does not exist")
    for field, value in normalized.items():
        setattr(instruction, field, value)
    instruction.save()
    after_snapshot = snapshot_from_rows(_current_rows(lock=True))
    settings_obj.instruction_draft_revision = int(expected_revision) + 1
    settings_obj.save(update_fields=["instruction_draft_revision", "updated_at"])
    before_hash = snapshot_hash(before_snapshot)
    after_hash = snapshot_hash(after_snapshot)
    _audit(
        actor=actor,
        action="ig_bot.policy_draft_saved",
        entity_id=str(instruction.pk),
        before={"revision": int(expected_revision), "snapshot_hash": before_hash},
        after={
            "revision": int(settings_obj.instruction_draft_revision),
            "snapshot_hash": after_hash,
        },
        note=note,
    )
    return instruction, DraftState(
        revision=int(settings_obj.instruction_draft_revision),
        snapshot=after_snapshot,
        snapshot_hash=after_hash,
    )


@transaction.atomic
def delete_instruction_draft(
    *,
    expected_revision: int,
    expected_snapshot_hash: str,
    instruction_id: int,
    actor=None,
    note: str = "",
) -> DraftState:
    from management.models import BotInstruction

    settings_obj = _settings_lock()
    if int(settings_obj.instruction_draft_revision or 0) != int(expected_revision):
        raise DraftRevisionConflict()
    before_snapshot = snapshot_from_rows(_current_rows(lock=True))
    if snapshot_hash(before_snapshot) != str(expected_snapshot_hash or ""):
        raise DraftRevisionConflict()
    instruction = BotInstruction.objects.select_for_update().filter(pk=instruction_id).first()
    if instruction is None:
        raise PolicyPublicationError("instruction_not_found", "instruction does not exist")
    instruction.delete()
    after_snapshot = snapshot_from_rows(_current_rows(lock=True))
    settings_obj.instruction_draft_revision = int(expected_revision) + 1
    settings_obj.save(update_fields=["instruction_draft_revision", "updated_at"])
    after_hash = snapshot_hash(after_snapshot)
    _audit(
        actor=actor,
        action="ig_bot.policy_draft_deleted",
        entity_id=str(instruction_id),
        before={
            "revision": int(expected_revision),
            "snapshot_hash": snapshot_hash(before_snapshot),
        },
        after={
            "revision": int(settings_obj.instruction_draft_revision),
            "snapshot_hash": after_hash,
        },
        note=note,
    )
    return DraftState(
        revision=int(settings_obj.instruction_draft_revision),
        snapshot=after_snapshot,
        snapshot_hash=after_hash,
    )


def _head_matches(settings_obj, *, expected_head_id, expected_head_hash: str) -> object | None:
    from management.models import BotPolicyPublication

    actual_id = settings_obj.active_instruction_publication_id
    expected_id = int(expected_head_id) if expected_head_id is not None else None
    if actual_id != expected_id:
        raise PublicationHeadConflict()
    head = (
        BotPolicyPublication.objects.select_for_update().filter(pk=actual_id).first()
        if actual_id else None
    )
    actual_hash = str(getattr(head, "snapshot_hash", "") or "")
    if actual_hash != str(expected_head_hash or ""):
        raise PublicationHeadConflict()
    return head


def _validate_publishable(snapshot: dict) -> None:
    items = _snapshot_items(snapshot)
    if any(item.get("active") and not str(item.get("body") or "").strip() for item in items):
        raise PolicyPublicationError(
            "active_instruction_empty", "active instruction body cannot be empty"
        )
    # Use the real whole-module selector and budget behavior as publication
    # readiness, without customer or operator-only data.
    select_policy_snapshot(snapshot, public_only=True)


def _next_publication_version(model) -> int:
    from django.db.models import Max

    current = model.objects.aggregate(value=Max("version")).get("value")
    return int(current or 0) + 1


@transaction.atomic
def publish_instruction_policy(
    *,
    expected_draft_revision: int,
    expected_draft_hash: str,
    expected_head_id: int | None,
    expected_head_hash: str,
    actor=None,
    note: str = "",
) -> PublicationResult:
    from management.models import BotPolicyPublication

    settings_obj = _settings_lock()
    if int(settings_obj.instruction_draft_revision or 0) != int(expected_draft_revision):
        raise DraftRevisionConflict()
    head = _head_matches(
        settings_obj,
        expected_head_id=expected_head_id,
        expected_head_hash=expected_head_hash,
    )
    snapshot = snapshot_from_rows(_current_rows(lock=True))
    digest = snapshot_hash(snapshot)
    if digest != str(expected_draft_hash or ""):
        raise DraftRevisionConflict()
    _validate_publishable(snapshot)
    if (
        head is not None
        and head.snapshot_hash == digest
        and head.compiler_version == PUBLICATION_COMPILER_VERSION
    ):
        return PublicationResult(head, False)
    version = _next_publication_version(BotPolicyPublication)
    publication = BotPolicyPublication.objects.create(
        version=version,
        kind=BotPolicyPublication.Kind.PUBLISH,
        parent=head,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot=snapshot,
        snapshot_hash=digest,
        compiler_version=PUBLICATION_COMPILER_VERSION,
        instruction_count=len(snapshot["instructions"]),
        actor=actor if getattr(actor, "pk", None) else None,
        actor_label=_actor_label(actor),
        note=str(note or "")[:500],
    )
    settings_obj.active_instruction_publication = publication
    settings_obj.settings_revision = int(settings_obj.settings_revision or 0) + 1
    settings_obj.reply_permission_epoch = int(settings_obj.reply_permission_epoch or 0) + 1
    settings_obj.save(update_fields=[
        "active_instruction_publication",
        "settings_revision",
        "reply_permission_epoch",
        "updated_at",
    ])
    _audit(
        actor=actor,
        action="ig_bot.policy_published",
        entity_id=str(publication.pk),
        before={
            "head_id": getattr(head, "pk", None),
            "snapshot_hash": str(getattr(head, "snapshot_hash", "") or ""),
        },
        after={
            "head_id": publication.pk,
            "version": publication.version,
            "snapshot_hash": digest,
            "instruction_count": publication.instruction_count,
        },
        note=note,
    )
    return PublicationResult(publication, True)


@transaction.atomic
def rollback_instruction_policy(
    *,
    target_publication_id: int,
    expected_head_id: int,
    expected_head_hash: str,
    actor=None,
    note: str = "",
) -> PublicationResult:
    from management.models import BotPolicyPublication

    settings_obj = _settings_lock()
    head = _head_matches(
        settings_obj,
        expected_head_id=expected_head_id,
        expected_head_hash=expected_head_hash,
    )
    target = BotPolicyPublication.objects.filter(pk=target_publication_id).first()
    if head is None or target is None:
        raise PolicyPublicationError("publication_not_found", "publication does not exist")
    if snapshot_hash(target.snapshot) != target.snapshot_hash:
        raise PolicyPublicationError("rollback_target_hash_mismatch", "rollback target failed integrity validation")
    _validate_publishable(target.snapshot)
    publication = BotPolicyPublication.objects.create(
        version=_next_publication_version(BotPolicyPublication),
        kind=BotPolicyPublication.Kind.ROLLBACK,
        parent=head,
        restored_from=target,
        schema_version=target.schema_version,
        snapshot=target.snapshot,
        snapshot_hash=target.snapshot_hash,
        compiler_version=target.compiler_version,
        instruction_count=target.instruction_count,
        actor=actor if getattr(actor, "pk", None) else None,
        actor_label=_actor_label(actor),
        note=str(note or "")[:500],
    )
    settings_obj.active_instruction_publication = publication
    settings_obj.settings_revision = int(settings_obj.settings_revision or 0) + 1
    settings_obj.reply_permission_epoch = int(settings_obj.reply_permission_epoch or 0) + 1
    settings_obj.save(update_fields=[
        "active_instruction_publication",
        "settings_revision",
        "reply_permission_epoch",
        "updated_at",
    ])
    _audit(
        actor=actor,
        action="ig_bot.policy_rolled_back",
        entity_id=str(publication.pk),
        before={"head_id": head.pk, "snapshot_hash": head.snapshot_hash},
        after={
            "head_id": publication.pk,
            "version": publication.version,
            "snapshot_hash": publication.snapshot_hash,
            "restored_from": target.pk,
        },
        note=note,
    )
    return PublicationResult(publication, True)


__all__ = [
    "ACTUAL_PROPOSAL_CONSUMERS",
    "ActivePolicySnapshot",
    "BASELINE_PROPOSAL_ACTIONS",
    "DEFAULT_INSTRUCTION_BUDGET_CHARS",
    "DraftRevisionConflict",
    "DraftState",
    "PolicyPublicationError",
    "PublicationHeadConflict",
    "PublicationResult",
    "PROPOSAL_VISIBILITY_ACTIONS",
    "PUBLICATION_COMPILER_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "delete_instruction_draft",
    "draft_state",
    "load_active_policy_snapshot",
    "preview_instruction_draft",
    "publication_history",
    "publish_instruction_policy",
    "rollback_instruction_policy",
    "save_instruction_draft",
    "select_policy_snapshot",
    "snapshot_from_rows",
    "snapshot_hash",
]
