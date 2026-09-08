"""Dormant B03.4/B03.5 outbox for one physical provider request per row."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable, Mapping

from django.db import transaction
from django.utils import timezone

from management.models import (
    IgClient,
    IgCustomerTurnRevision,
    IgRevisionDeliveryEffect,
    IgWebhookInboxEvent,
    InstagramBotMessage,
    InstagramBotSettings,
)


ACTOR_BOT = "bot"
PURPOSE_NORMAL_REPLY = "normal_reply"
MAX_EFFECTS = 16
MAX_PAYLOAD_BYTES = 256 * 1024
KNOWN_NO_DISPATCH_REASONS = frozenset({
    "transport_preflight_transaction_active",
    "transport_preflight_namespace_mismatch",
    "transport_preflight_credentials_unavailable",
    "transport_preflight_recipient_invalid",
    "transport_preflight_payload_invalid",
    "transport_preflight_payload_too_large",
    "transport_preflight_url_invalid",
    "transport_preflight_check_failed",
})
CLAIM_LEASE_SECONDS = 60
GROUP_KINDS = {
    "catalog_media": frozenset({"image"}),
    "substantive_text": frozenset({"text"}),
    "template": frozenset({"template"}),
    "template_fallback": frozenset({"fallback"}),
}
_FORBIDDEN_PAYLOAD_KEYS = frozenset({
    "access_token", "accesstoken", "api_key", "apikey", "authorization",
    "authorization_header", "credential", "credentials", "header", "headers",
    "password", "secret", "client_secret", "clientsecret", "token",
    "x_api_key", "x_goog_api_key",
})
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TERMINAL_STATES = frozenset({
    "sent", "definite_failed", "unknown", "cancelled", "superseded",
})


@dataclass(frozen=True)
class PublicationBinding:
    publication_id: int
    version: int
    snapshot_hash: str


@dataclass(frozen=True)
class OutboxReadiness:
    ready: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectPlanResult:
    effects: tuple[IgRevisionDeliveryEffect, ...] = ()
    created: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class EffectClaim:
    effect: IgRevisionDeliveryEffect | None
    token: str = ""
    reason: str = ""


@dataclass(frozen=True)
class EffectTransition:
    effect: IgRevisionDeliveryEffect | None
    changed: bool
    reason: str = ""


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _append(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _contains_forbidden_key(value) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = str(key or "").strip().casefold().replace("-", "_")
            if normalized_key in _FORBIDDEN_PAYLOAD_KEYS:
                return True
            if _contains_forbidden_key(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _contains_url(value) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_url(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_url(item) for item in value)
    return isinstance(value, str) and value.strip().casefold().startswith(
        ("http://", "https://")
    )


def _safe_bindings(value: Iterable[Mapping] | None) -> list[dict]:
    items = list(value or ())
    if len(items) > 16:
        raise ValueError("binding_limit")
    output = []
    for item in items:
        if (
            not isinstance(item, Mapping)
            or _contains_forbidden_key(item)
            or _contains_url(item)
        ):
            raise ValueError("invalid_binding")
        encoded = _canonical(dict(item))
        if len(encoded) > 4096:
            raise ValueError("binding_too_large")
        output.append(dict(item))
    return output


def _revision_namespace(revision) -> str:
    namespaces = set(
        revision.sources.exclude(source_namespace="")
        .values_list("source_namespace", flat=True)
    )
    return namespaces.pop() if len(namespaces) == 1 else ""


def _active_opt_out(client) -> bool:
    return bool(
        client.opted_out_at
        and (not client.opted_in_at or client.opted_out_at > client.opted_in_at)
    )


def _run_checker(
    checker: Callable | None,
    bindings: list[dict],
    *,
    revision,
    client,
    settings_obj,
) -> bool:
    if not bindings:
        return True
    if checker is None:
        return False
    try:
        return checker(
            bindings,
            revision=revision,
            client=client,
            settings_obj=settings_obj,
        ) is True
    except Exception:
        return False


def _normal_reply_window_deadline(revision):
    from management.services.ig_ai_reply_recovery import RESPONSE_WINDOW
    from management.services.ig_turn_revisions import MAX_SOURCES

    timestamps = list(revision.sources.filter(
        role=InstagramBotMessage.Role.USER,
        message__role=InstagramBotMessage.Role.USER,
        message__client_id=revision.client_id,
    ).values_list("provider_created_at", "message__created_at")[:MAX_SOURCES + 1])
    if not timestamps or len(timestamps) > MAX_SOURCES or len(timestamps) != revision.source_count:
        return None
    anchors = [provider_at or received_at for provider_at, received_at in timestamps]
    if any(value is None for value in anchors):
        return None
    return max(anchors) + RESPONSE_WINDOW


def _cas_readiness(
    revision,
    *,
    client,
    settings_obj,
    revision_token: str,
    settings_id: int,
    settings_permission_epoch: int,
    publication: PublicationBinding,
    fact_bindings: list[dict],
    offer_bindings: list[dict],
    fact_checker=None,
    offer_checker=None,
    now=None,
) -> OutboxReadiness:
    now = now or timezone.now()
    reasons: list[str] = []
    if (
        revision.active_slot != 1
        or revision.state != revision.State.CLAIMED
        or not revision_token
        or revision.claim_token != revision_token
        or not revision.lease_until
        or revision.lease_until <= now
        or not _HASH_RE.fullmatch(str(revision.snapshot_digest or ""))
    ):
        _append(reasons, "revision_not_current")
    elif _digest(revision.bundle_snapshot) != revision.snapshot_digest:
        _append(reasons, "revision_snapshot_invalid")
    if revision.overall_deadline <= now:
        from management.services.ig_revision_recovery import execution_resume_is_current

        if not execution_resume_is_current(revision, now=now):
            _append(reasons, "revision_deadline_exhausted")
    window_deadline = _normal_reply_window_deadline(revision)
    if window_deadline is None:
        _append(reasons, "reply_window_unavailable")
    elif window_deadline <= now:
        _append(reasons, "reply_window_closed")
    if client is None:
        _append(reasons, "client_missing")
    else:
        if int(client.reply_permission_epoch or 0) != int(revision.permission_epoch or 0):
            _append(reasons, "client_permission_changed")
        if (
            client.privacy_erasure_started_at != revision.erasure_started_at_snapshot
            or client.privacy_erasure_started_at is not None
        ):
            _append(reasons, "client_erasure_changed")
        if client.hidden_at or client.is_blocked or client.bot_paused:
            _append(reasons, "client_blocked")
        if client.manager_takeover:
            _append(reasons, "manager_takeover")
        if _active_opt_out(client):
            _append(reasons, "client_opted_out")
    if settings_obj is None or settings_obj.pk != settings_id or not settings_obj.is_enabled:
        _append(reasons, "settings_disabled")
    elif int(settings_obj.reply_permission_epoch or 0) != int(settings_permission_epoch):
        _append(reasons, "settings_permission_changed")
    active_publication = (
        getattr(settings_obj, "active_instruction_publication", None)
        if settings_obj else None
    )
    if (
        active_publication is None
        or active_publication.pk != int(publication.publication_id or 0)
        or int(active_publication.version or 0) != int(publication.version or 0)
        or str(active_publication.snapshot_hash or "") != str(publication.snapshot_hash or "")
    ):
        _append(reasons, "publication_changed")
    namespace = _revision_namespace(revision)
    if not namespace:
        _append(reasons, "revision_namespace_unavailable")
    elif client is not None and IgWebhookInboxEvent.objects.filter(
        namespace=namespace,
        customer_igsid=client.igsid,
        decision__in=(
            IgWebhookInboxEvent.Decision.ACCEPTED,
            IgWebhookInboxEvent.Decision.BLOCKED,
        ),
        processed_at__isnull=True,
    ).exists():
        _append(reasons, "pending_inbound")
    if client is not None and settings_obj is not None:
        from management.services.ig_permission_transitions import permission_transition_blocks
        from management.services.ig_revision_echo import revision_echo_blocks
        from management.services.instagram_bot import allowed_sender_ids

        if permission_transition_blocks(settings_id=settings_obj.pk, client_id=client.pk):
            _append(reasons, "permission_transition_pending")
        if namespace and revision_echo_blocks(client.pk, namespace):
            _append(reasons, "echo_attribution_pending")
        allowlist = allowed_sender_ids(settings_obj)
        if allowlist and client.igsid not in allowlist:
            _append(reasons, "sender_not_allowed")
        if not _run_checker(
            fact_checker,
            fact_bindings,
            revision=revision,
            client=client,
            settings_obj=settings_obj,
        ):
            _append(reasons, "fact_binding_unavailable")
        if not _run_checker(
            offer_checker,
            offer_bindings,
            revision=revision,
            client=client,
            settings_obj=settings_obj,
        ):
            _append(reasons, "offer_binding_unavailable")
    return OutboxReadiness(not reasons, tuple(reasons))


def pre_winner_readiness(
    revision_id: int,
    revision_token: str,
    *,
    settings_id: int,
    settings_permission_epoch: int,
    publication: PublicationBinding,
    fact_bindings=(),
    offer_bindings=(),
    fact_checker=None,
    offer_checker=None,
    now=None,
) -> OutboxReadiness:
    """DB-only hook; caller may use failure to request one in-budget repair."""
    try:
        facts = _safe_bindings(fact_bindings)
        offers = _safe_bindings(offer_bindings)
    except ValueError as exc:
        return OutboxReadiness(False, (str(exc),))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
        "client_id"
    ).first()
    if identity is None:
        return OutboxReadiness(False, ("revision_missing",))
    with transaction.atomic():
        settings_obj = (
            InstagramBotSettings.objects.select_for_update()
            .select_related("active_instruction_publication")
            .filter(pk=settings_id)
            .first()
        )
        client = IgClient.objects.select_for_update().filter(
            pk=identity["client_id"]
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, client_id=identity["client_id"]
        ).first()
        if revision is None:
            return OutboxReadiness(False, ("revision_missing",))
        return _cas_readiness(
            revision,
            client=client,
            settings_obj=settings_obj,
            revision_token=revision_token,
            settings_id=settings_id,
            settings_permission_epoch=settings_permission_epoch,
            publication=publication,
            fact_bindings=facts,
            offer_bindings=offers,
            fact_checker=fact_checker,
            offer_checker=offer_checker,
            now=now,
        )


def _normalize_specs(specs, recipient: str) -> list[dict]:
    raw_specs = list(specs or ())
    if not raw_specs or len(raw_specs) > MAX_EFFECTS:
        raise ValueError("effect_count_invalid")
    counts: dict[str, int] = {}
    output = []
    for order_index, raw in enumerate(raw_specs):
        if not isinstance(raw, Mapping):
            raise ValueError("effect_invalid")
        group = str(raw.get("group") or "")
        kind = str(raw.get("kind") or "")
        if group not in GROUP_KINDS or kind not in GROUP_KINDS[group]:
            raise ValueError("effect_kind_invalid")
        payload = raw.get("payload")
        if not isinstance(payload, Mapping) or _contains_forbidden_key(payload):
            raise ValueError("effect_payload_invalid")
        payload = dict(payload)
        payload_recipient = payload.get("recipient")
        payload_recipient = (
            payload_recipient.get("id")
            if isinstance(payload_recipient, Mapping) else None
        )
        if str(payload_recipient or "") != recipient:
            raise ValueError("effect_recipient_mismatch")
        if len(_canonical(payload)) > MAX_PAYLOAD_BYTES:
            raise ValueError("effect_payload_too_large")
        part_index = counts.get(group, 0)
        counts[group] = part_index + 1
        projection = raw.get("projection_metadata") or {}
        if not isinstance(projection, Mapping):
            raise ValueError("effect_projection_invalid")
        projection = dict(projection)
        if projection:
            if group != "catalog_media" or set(projection) != {"part_index", "product_id", "title"}:
                raise ValueError("effect_projection_invalid")
            if (
                isinstance(projection["product_id"], bool)
                or not isinstance(projection["product_id"], int)
                or projection["product_id"] <= 0
                or isinstance(projection["part_index"], bool)
                or projection["part_index"] != part_index
                or not isinstance(projection["title"], str)
                or len(projection["title"]) > 200
            ):
                raise ValueError("effect_projection_invalid")
        output.append({
            "group": group,
            "kind": kind,
            "order_index": order_index,
            "part_index": part_index,
            "payload": payload,
            "payload_digest": _digest(payload),
            "projection_metadata": projection,
            "projection_digest": _digest(projection),
            "activation": dict(raw.get("activation") or {}),
        })
    for item in output:
        item["part_count"] = counts[item["group"]]
    by_identity = {
        (item["group"], item["part_index"]): item for item in output
    }
    for item in output:
        activation = item.pop("activation")
        if item["group"] == "template_fallback":
            group = str(activation.get("group") or "")
            try:
                part_index = int(activation.get("part_index"))
            except (TypeError, ValueError):
                raise ValueError("fallback_activation_invalid")
            failure_code = str(activation.get("failure_code") or "")
            target = by_identity.get((group, part_index))
            if (
                target is None
                or target["order_index"] >= item["order_index"]
                or group == "template_fallback"
                or failure_code not in {"provider_rejected", "link_rejected"}
            ):
                raise ValueError("fallback_activation_invalid")
            item.update({
                "activation_group": group,
                "activation_part_index": part_index,
                "activation_failure_code": failure_code,
            })
        elif activation:
            raise ValueError("unexpected_effect_activation")
        else:
            item.update({
                "activation_group": "",
                "activation_part_index": None,
                "activation_failure_code": "",
            })
    return output


def plan_revision_effects(
    revision_id: int,
    revision_token: str,
    *,
    source_message_id: int,
    settings_id: int,
    settings_permission_epoch: int,
    publication: PublicationBinding,
    authority_context_digest: str,
    effects,
    fact_bindings=(),
    offer_bindings=(),
    fact_checker=None,
    offer_checker=None,
    generation_request_id: str = "",
    generation_model: str = "",
    actor: str = ACTOR_BOT,
    purpose: str = PURPOSE_NORMAL_REPLY,
    now=None,
) -> EffectPlanResult:
    """Atomically validate the winner and persist its complete physical plan."""
    if actor != ACTOR_BOT or purpose != PURPOSE_NORMAL_REPLY:
        return EffectPlanResult(reasons=("unsupported_actor_purpose",))
    if not _HASH_RE.fullmatch(str(authority_context_digest or "")):
        return EffectPlanResult(reasons=("authority_digest_invalid",))
    try:
        facts = _safe_bindings(fact_bindings)
        offers = _safe_bindings(offer_bindings)
    except ValueError as exc:
        return EffectPlanResult(reasons=(str(exc),))
    identity = IgCustomerTurnRevision.objects.filter(pk=revision_id).values(
        "client_id"
    ).first()
    if identity is None:
        return EffectPlanResult(reasons=("revision_missing",))
    with transaction.atomic():
        settings_obj = (
            InstagramBotSettings.objects.select_for_update()
            .select_related("active_instruction_publication")
            .filter(pk=settings_id)
            .first()
        )
        client = IgClient.objects.select_for_update().filter(
            pk=identity["client_id"]
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, client_id=identity["client_id"]
        ).first()
        if revision is None:
            return EffectPlanResult(reasons=("revision_missing",))
        if client is None:
            return EffectPlanResult(reasons=("client_missing",))
        try:
            specs = _normalize_specs(effects, client.igsid)
        except ValueError as exc:
            return EffectPlanResult(reasons=(str(exc),))
        if not revision.sources.filter(message_id=source_message_id).exists():
            return EffectPlanResult(reasons=("source_message_not_in_revision",))
        readiness = _cas_readiness(
            revision,
            client=client,
            settings_obj=settings_obj,
            revision_token=revision_token,
            settings_id=settings_id,
            settings_permission_epoch=settings_permission_epoch,
            publication=publication,
            fact_bindings=facts,
            offer_bindings=offers,
            fact_checker=fact_checker,
            offer_checker=offer_checker,
            now=now,
        )
        if not readiness.ready:
            return EffectPlanResult(reasons=readiness.reasons)
        plan_material = {
            "revision_id": revision.pk,
            "revision_snapshot_digest": revision.snapshot_digest,
            "actor": actor,
            "purpose": purpose,
            "effects": specs,
            "publication": {
                "id": publication.publication_id,
                "version": publication.version,
                "hash": publication.snapshot_hash,
            },
            "authority_context_digest": authority_context_digest,
            "fact_bindings": facts,
            "offer_bindings": offers,
        }
        plan_digest = _digest(plan_material)
        existing = tuple(revision.delivery_effects.order_by("order_index", "id"))
        if existing:
            if all(row.plan_digest == plan_digest for row in existing):
                return EffectPlanResult(existing, False)
            return EffectPlanResult(reasons=("plan_conflict",))
        namespace = _revision_namespace(revision)
        rows = []
        for item in specs:
            key_material = (
                f"{revision.pk}:{item['group']}:{item['part_index']}:"
                f"{item['payload_digest']}"
            )
            effect_key = "revfx:" + hashlib.sha256(key_material.encode()).hexdigest()
            rows.append(IgRevisionDeliveryEffect(
                revision=revision,
                source_message_id=source_message_id,
                effect_key=effect_key,
                actor=actor,
                purpose=purpose,
                group=item["group"],
                kind=item["kind"],
                order_index=item["order_index"],
                part_index=item["part_index"],
                part_count=item["part_count"],
                plan_digest=plan_digest,
                payload=item["payload"],
                payload_digest=item["payload_digest"],
                projection_metadata=item["projection_metadata"],
                projection_digest=item["projection_digest"],
                activation_group=item["activation_group"],
                activation_part_index=item["activation_part_index"],
                activation_failure_code=item["activation_failure_code"],
                recipient_igsid=client.igsid,
                provider_namespace=namespace,
                generation_request_id=str(generation_request_id or "")[:40],
                generation_model=str(generation_model or "")[:80],
                settings_id_snapshot=settings_id,
                settings_permission_epoch=settings_permission_epoch,
                client_permission_epoch=revision.permission_epoch,
                revision_snapshot_digest=revision.snapshot_digest,
                publication_id=publication.publication_id,
                publication_version=publication.version,
                publication_hash=publication.snapshot_hash,
                authority_context_digest=authority_context_digest,
                fact_bindings=facts,
                offer_bindings=offers,
            ))
        IgRevisionDeliveryEffect.objects.bulk_create(rows)
        return EffectPlanResult(
            tuple(revision.delivery_effects.order_by("order_index", "id")), True
        )


def claim_next_effect(
    revision_id: int,
    revision_token: str,
    group: str,
    *,
    now=None,
) -> EffectClaim:
    """Claim the next unsent part in one group; other groups remain independent."""
    if group not in GROUP_KINDS:
        return EffectClaim(None, reason="group_invalid")
    now = now or timezone.now()
    identity = (
        IgRevisionDeliveryEffect.objects.filter(
            revision_id=revision_id, group=group
        )
        .order_by("part_index", "id")
        .values("settings_id_snapshot", "revision__client_id")
        .first()
    )
    if identity is None:
        return EffectClaim(None, reason="group_complete")
    with transaction.atomic():
        settings_obj = InstagramBotSettings.objects.select_for_update().filter(
            pk=identity["settings_id_snapshot"]
        ).first()
        client = IgClient.objects.select_for_update().filter(
            pk=identity["revision__client_id"]
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=revision_id, client_id=identity["revision__client_id"]
        ).first()
        if (
            settings_obj is None
            or client is None
            or revision is None
            or revision.active_slot != 1
            or revision.state != revision.State.CLAIMED
            or revision.claim_token != revision_token
            or not revision.lease_until
            or revision.lease_until <= now
        ):
            return EffectClaim(None, reason="revision_not_current")
        locked_effects = list(
            IgRevisionDeliveryEffect.objects.select_for_update()
            .filter(revision=revision)
            .order_by("order_index", "id")
        )
        rows = [row for row in locked_effects if row.group == group]
        by_identity = {
            (row.group, row.part_index): row for row in locked_effects
        }
        for row in rows:
            if row.state == row.State.SENT:
                continue
            if row.state == row.State.CLAIMED and row.lease_until and row.lease_until <= now:
                pass
            elif row.state != row.State.PLANNED:
                return EffectClaim(row, reason="group_blocked")
            if row.activation_group:
                primary = by_identity.get(
                    (row.activation_group, row.activation_part_index)
                )
                active = bool(
                    primary
                    and primary.state == primary.State.DEFINITE_FAILED
                    and primary.failure_code == row.activation_failure_code
                )
                if not active:
                    if primary and primary.state in {
                        primary.State.SENT,
                        primary.State.UNKNOWN,
                        primary.State.CANCELLED,
                        primary.State.SUPERSEDED,
                    }:
                        row.state = row.State.CANCELLED
                        row.failure_code = "activation_not_applicable"
                        row.terminal_at = now
                        row.save(update_fields=[
                            "state", "failure_code", "terminal_at", "updated_at",
                        ])
                    return EffectClaim(row, reason="activation_blocked")
            token = secrets.token_hex(16)
            row.state = row.State.CLAIMED
            row.claim_token = token
            row.lease_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
            row.attempts = int(row.attempts or 0) + 1
            row.save(update_fields=[
                "state", "claim_token", "lease_until", "attempts", "updated_at",
            ])
            return EffectClaim(row, token, "claimed")
        return EffectClaim(None, reason="group_complete")


def mark_provider_started(
    effect_id: int,
    effect_token: str,
    revision_token: str,
    *,
    fact_checker=None,
    offer_checker=None,
    now=None,
) -> EffectTransition:
    """Repeat final CAS and commit the non-cancellable boundary before socket I/O."""
    now = now or timezone.now()
    identity = IgRevisionDeliveryEffect.objects.filter(pk=effect_id).values(
        "settings_id_snapshot", "revision_id", "revision__client_id"
    ).first()
    if identity is None:
        return EffectTransition(None, False, "effect_claim_lost")
    with transaction.atomic():
        settings_obj = (
            InstagramBotSettings.objects.select_for_update()
            .select_related("active_instruction_publication")
            .filter(pk=identity["settings_id_snapshot"])
            .first()
        )
        client = IgClient.objects.select_for_update().filter(
            pk=identity["revision__client_id"]
        ).first()
        revision = IgCustomerTurnRevision.objects.select_for_update().filter(
            pk=identity["revision_id"], client_id=identity["revision__client_id"]
        ).first()
        effect = IgRevisionDeliveryEffect.objects.select_for_update().filter(
            pk=effect_id,
            revision_id=identity["revision_id"],
            settings_id_snapshot=identity["settings_id_snapshot"],
        ).first()
        if (
            effect is None
            or revision is None
            or client is None
            or settings_obj is None
            or effect.state != effect.State.CLAIMED
            or not effect_token
            or effect.claim_token != effect_token
            or not effect.lease_until
            or effect.lease_until <= now
        ):
            return EffectTransition(effect, False, "effect_claim_lost")
        revision_namespace = _revision_namespace(revision)
        immutable_request_valid = bool(
            effect.actor == ACTOR_BOT
            and effect.purpose == PURPOSE_NORMAL_REPLY
            and effect.recipient_igsid == client.igsid
            and effect.provider_namespace == revision_namespace
            and effect.client_permission_epoch == revision.permission_epoch
            and _digest(effect.payload) == effect.payload_digest
            and (
                effect.projection_digest == _digest(effect.projection_metadata)
                or (not effect.projection_metadata and not effect.projection_digest)
            )
            and effect.revision_snapshot_digest == revision.snapshot_digest
        )
        if not immutable_request_valid:
            effect.state = effect.State.CANCELLED
            effect.failure_code = "effect_binding_invalid"
            effect.terminal_at = now
            effect.claim_token = ""
            effect.lease_until = None
            effect.save(update_fields=[
                "state", "failure_code", "terminal_at", "claim_token",
                "lease_until", "updated_at",
            ])
            _terminalize_inactive_dependents(effect, now=now)
            return EffectTransition(effect, True, effect.failure_code)
        publication = PublicationBinding(
            effect.publication_id,
            effect.publication_version,
            effect.publication_hash,
        )
        readiness = _cas_readiness(
            revision,
            client=client,
            settings_obj=settings_obj,
            revision_token=revision_token,
            settings_id=effect.settings_id_snapshot,
            settings_permission_epoch=effect.settings_permission_epoch,
            publication=publication,
            fact_bindings=list(effect.fact_bindings or []),
            offer_bindings=list(effect.offer_bindings or []),
            fact_checker=fact_checker,
            offer_checker=offer_checker,
            now=now,
        )
        if not readiness.ready:
            effect.state = (
                effect.State.SUPERSEDED
                if "revision_not_current" in readiness.reasons
                else effect.State.CANCELLED
            )
            effect.failure_code = readiness.reasons[0]
            effect.terminal_at = now
            effect.claim_token = ""
            effect.lease_until = None
            effect.save(update_fields=[
                "state", "failure_code", "terminal_at", "claim_token",
                "lease_until", "updated_at",
            ])
            _terminalize_inactive_dependents(effect, now=now)
            return EffectTransition(effect, True, effect.failure_code)
        effect.state = effect.State.PROVIDER_STARTED
        effect.provider_started_at = now
        effect.lease_until = None
        effect.save(update_fields=[
            "state", "provider_started_at", "lease_until", "updated_at",
        ])
        return EffectTransition(effect, True, "provider_started")


def cancel_unstarted_effect(
    effect_id: int,
    revision_token: str,
    *,
    effect_token: str = "",
    reason: str = "cancelled",
) -> bool:
    """Cancel only before provider-started; never mask possible delivery."""
    code = str(reason or "cancelled").casefold()
    if not _SAFE_CODE_RE.fullmatch(code):
        code = "cancelled"
    with transaction.atomic():
        effect = (
            IgRevisionDeliveryEffect.objects.select_for_update()
            .select_related("revision")
            .filter(pk=effect_id)
            .first()
        )
        if (
            effect is None
            or not revision_token
            or effect.revision.claim_token != revision_token
            or effect.state not in {
                effect.State.PLANNED,
                effect.State.CLAIMED,
            }
            or (
                effect.state == effect.State.CLAIMED
                and effect.claim_token != effect_token
            )
        ):
            return False
        effect.state = effect.State.CANCELLED
        effect.failure_code = code
        effect.terminal_at = timezone.now()
        effect.claim_token = ""
        effect.lease_until = None
        effect.save(update_fields=[
            "state", "failure_code", "terminal_at", "claim_token",
            "lease_until", "updated_at",
        ])
        _terminalize_inactive_dependents(effect, now=effect.terminal_at)
        return True


def finish_effect(
    effect_id: int,
    effect_token: str,
    *,
    provider_namespace: str,
    http_status: int | None = None,
    provider_message_id: str = "",
    transport_outcome: str = "response",
    explicit_rejection_code: str = "provider_rejected",
    response_digest: str = "",
    now=None,
) -> EffectTransition:
    """Classify one started request without error text or blind retry."""
    now = now or timezone.now()
    with transaction.atomic():
        effect = IgRevisionDeliveryEffect.objects.select_for_update().filter(
            pk=effect_id
        ).first()
        if (
            effect is None
            or effect.state != effect.State.PROVIDER_STARTED
            or effect.claim_token != effect_token
        ):
            return EffectTransition(effect, False, "effect_not_started")
        if str(provider_namespace or "") != effect.provider_namespace:
            state, failure = effect.State.UNKNOWN, "receipt_namespace_mismatch"
        else:
            try:
                status = int(http_status) if http_status is not None else None
            except (TypeError, ValueError):
                status = None
            provider_id = str(provider_message_id or "").strip()
            valid_provider_id = bool(
                0 < len(provider_id) <= 255
                and all(ord(char) >= 32 and ord(char) != 127 for char in provider_id)
            )
            outcome = str(transport_outcome or "").casefold()
            if outcome == "known_not_dispatched":
                rejection = str(explicit_rejection_code or "").casefold()
                if (
                    http_status is None and not provider_id and not response_digest
                    and rejection in KNOWN_NO_DISPATCH_REASONS
                ):
                    state, failure = effect.State.DEFINITE_FAILED, rejection
                else:
                    state, failure = effect.State.UNKNOWN, "no_dispatch_evidence_invalid"
            elif status is not None and 200 <= status < 300 and valid_provider_id:
                state, failure = effect.State.SENT, ""
            elif status is not None and 200 <= status < 300:
                state, failure = effect.State.UNKNOWN, "provider_message_id_missing"
            elif outcome == "explicit_rejected" and status is not None and 400 <= status < 500:
                rejection = str(explicit_rejection_code or "").casefold()
                if rejection not in {"provider_rejected", "link_rejected"}:
                    rejection = "provider_rejected"
                state, failure = effect.State.DEFINITE_FAILED, rejection
            elif status is not None and status >= 500:
                state, failure = effect.State.UNKNOWN, "provider_5xx"
            elif outcome in {"timeout", "exception", "disconnect"}:
                state, failure = effect.State.UNKNOWN, "provider_transport_unknown"
            else:
                state, failure = effect.State.UNKNOWN, "provider_result_unknown"
            effect.provider_message_id = provider_id if state == effect.State.SENT else ""
            effect.provider_http_status = status
        effect.state = state
        effect.failure_code = failure
        effect.provider_response_digest = (
            str(response_digest).casefold()
            if _HASH_RE.fullmatch(str(response_digest or "").casefold())
            else ""
        )
        effect.terminal_at = now
        effect.claim_token = ""
        effect.lease_until = None
        effect.save(update_fields=[
            "state", "failure_code", "provider_message_id",
            "provider_http_status", "provider_response_digest", "terminal_at",
            "claim_token", "lease_until", "updated_at",
        ])
        _terminalize_inactive_dependents(effect, now=now)
    project_legacy_message(effect.revision_id)
    return EffectTransition(effect, True, effect.state)


def _terminalize_inactive_dependents(effect, *, now) -> int:
    """Close conditional fallbacks unless the exact rejection activated them."""
    active_failure = bool(
        effect.state == effect.State.DEFINITE_FAILED
        and effect.failure_code in {"provider_rejected", "link_rejected"}
    )
    dependents = IgRevisionDeliveryEffect.objects.filter(
        revision_id=effect.revision_id,
        activation_group=effect.group,
        activation_part_index=effect.part_index,
        state__in=(
            IgRevisionDeliveryEffect.State.PLANNED,
            IgRevisionDeliveryEffect.State.CLAIMED,
        ),
    )
    if active_failure:
        dependents = dependents.exclude(
            activation_failure_code=effect.failure_code
        )
    return dependents.update(
        state=IgRevisionDeliveryEffect.State.CANCELLED,
        failure_code="activation_not_applicable",
        terminal_at=now,
        claim_token="",
        lease_until=None,
        updated_at=now,
    )


def project_legacy_message(revision_id: int) -> bool:
    """Project canonical effects for old UI only; never write the legacy key."""
    effects = list(
        IgRevisionDeliveryEffect.objects.filter(revision_id=revision_id)
        .order_by("order_index", "id")
    )
    if not effects:
        return False
    source_id = effects[0].source_message_id
    if any(effect.source_message_id != source_id for effect in effects):
        return False
    states = {effect.state for effect in effects}
    sent = [effect for effect in effects if effect.state == effect.State.SENT]
    if IgRevisionDeliveryEffect.State.UNKNOWN in states:
        aggregate = "unknown"
    elif IgRevisionDeliveryEffect.State.PROVIDER_STARTED in states:
        aggregate = "sending"
    elif states <= {IgRevisionDeliveryEffect.State.SENT}:
        aggregate = "sent"
    elif sent and states.intersection(_TERMINAL_STATES - {"sent"}):
        aggregate = "unknown"
    elif IgRevisionDeliveryEffect.State.DEFINITE_FAILED in states:
        aggregate = "failed"
    elif states <= {
        IgRevisionDeliveryEffect.State.CANCELLED,
        IgRevisionDeliveryEffect.State.SUPERSEDED,
    }:
        aggregate = "cancelled"
    elif states.intersection({
        IgRevisionDeliveryEffect.State.CLAIMED,
        IgRevisionDeliveryEffect.State.PLANNED,
    }):
        aggregate = "sending"
    else:
        aggregate = "unknown"
    provider_ids = [effect.provider_message_id for effect in sent]
    started_values = [
        effect.provider_started_at for effect in effects if effect.provider_started_at
    ]
    terminal_values = [effect.terminal_at for effect in effects if effect.terminal_at]
    failure = next((effect.failure_code for effect in effects if effect.failure_code), "")
    return bool(InstagramBotMessage.objects.filter(pk=source_id).update(
        send_state=aggregate,
        # Legacy reconciliation selects UNKNOWN rows with send_started_at and
        # resolves them by a weak text digest. Revision effects deliberately
        # omit that legacy selector timestamp; their canonical timestamp and
        # UNKNOWN debt remain on this effect table.
        send_started_at=(
            None
            if aggregate == "unknown"
            else min(started_values) if started_values else None
        ),
        send_completed_at=max(terminal_values) if terminal_values and aggregate == "sent" else None,
        delivery_planned_chunk_count=len(effects),
        delivery_delivered_chunk_count=len(sent),
        delivery_provider_message_ids=provider_ids,
        delivery_failure_boundary=failure,
    ))


def unknown_effects(*, limit: int = 100) -> list[dict]:
    """Read-only UNKNOWN debt; text matching is intentionally unavailable."""
    rows = IgRevisionDeliveryEffect.objects.filter(
        state=IgRevisionDeliveryEffect.State.UNKNOWN
    ).order_by("terminal_at", "id")[: max(1, min(int(limit), 500))]
    return [{
        "effect_id": row.pk,
        "revision_id": row.revision_id,
        "provider_namespace": row.provider_namespace,
        "failure_code": row.failure_code,
        "provider_started_at": (
            row.provider_started_at.isoformat() if row.provider_started_at else ""
        ),
    } for row in rows]


__all__ = [
    "ACTOR_BOT", "PURPOSE_NORMAL_REPLY", "EffectClaim", "EffectPlanResult",
    "EffectTransition", "OutboxReadiness", "PublicationBinding",
    "cancel_unstarted_effect", "claim_next_effect", "finish_effect",
    "mark_provider_started", "plan_revision_effects", "pre_winner_readiness",
    "project_legacy_message", "unknown_effects",
]
